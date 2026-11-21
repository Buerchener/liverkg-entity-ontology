"""Shared read-only EMBL-EBI OLS transport for MCP and REST."""

from __future__ import annotations

import json
import logging
import math
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

LOG = logging.getLogger(__name__)
REST_URL = "https://www.ebi.ac.uk/ols4/api"
MCP_URL = REST_URL + "/mcp"
PROTOCOL = "2025-03-26"
OBO = "http://purl.obolibrary.org/obo/"
IAO_DEFINITION = OBO + "IAO_0000115"
OBO_IN_OWL = "http://www.geneontology.org/formats/oboInOwl#"
EXACT_SYNONYM = OBO_IN_OWL + "hasExactSynonym"
REQUIRED_TOOLS = {
    "searchClasses": {"query", "ontologyId", "includeObsoleteEntities", "pageNum", "pageSize"},
    "getChildren": {"ontologyId", "classIri", "pageNum", "pageSize"},
    "getAncestors": {"ontologyId", "classIri", "pageNum", "pageSize"},
}
MAX_BYTES = 4 * 1024 * 1024


class OLSError(RuntimeError):
    """A failed or unverifiable lookup. Callers should record the failed action."""


def _values(raw):
    """Strings from a single named OWL annotation, including axiom annotations."""
    values = raw if isinstance(raw, list) else [raw]
    texts = []
    for item in values:
        if isinstance(item, dict):
            kinds = item.get("type")
            if isinstance(kinds, list) and "reification" in kinds:
                item = item.get("value")
        if isinstance(item, str) and item.strip() and item not in texts:
            texts.append(item)
    return texts


def _array(value, field):
    if not isinstance(value, list):
        raise OLSError(f"Missing or malformed OLS field {field}")
    return value


def _read(request, timeout):
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise OLSError("OLS response exceeded the byte limit")
            return response.headers, raw.decode("utf-8")
    except HTTPError as exc:
        raise OLSError(f"OLS HTTP {exc.code}") from exc
    except (URLError, OSError, UnicodeError) as exc:
        raise OLSError(f"OLS transport failed: {type(exc).__name__}") from exc


def _json(raw):
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise OLSError("Invalid OLS JSON") from exc
    if not isinstance(obj, dict):
        raise OLSError("Expected an OLS JSON object")
    return obj


class OLSClient:
    """A synchronous shareable client. Discovery is lazy and serialized."""

    def __init__(self, mode="auto", *, timeout=12.0, page_size=8, mcp_url=MCP_URL, rest_url=REST_URL):
        if mode not in {"auto", "rest", "mcp"}:
            raise ValueError("mode must be auto, mcp, or rest")
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("timeout must be in (0, 120] seconds")
        if type(page_size) is not int or not 1 <= page_size <= 30:
            raise ValueError("page_size must be in [1, 30]")
        for endpoint in (mcp_url, rest_url):
            parts = urlsplit(endpoint)
            if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
                raise ValueError("OLS endpoint URLs must be credential-free base URLs")
        self.mode, self.timeout, self.page_size = mode, timeout, page_size
        self.mcp_url, self.rest_url = mcp_url, rest_url.rstrip("/")
        self._lock = threading.Lock()
        self._discovery = None
        self._session = None

    def discover(self):
        """Return a snapshot of the selected backend and discovery outcome."""
        with self._lock:
            if self._discovery is None:
                if self.mode == "rest":
                    self._discovery = {"transport": "rest", "endpoint": self.rest_url}
                else:
                    try:
                        self._discovery = self._discover_mcp()
                    except OLSError as exc:
                        if self.mode == "mcp":
                            raise
                        LOG.warning("OLS MCP discovery failed; using REST: %s", exc)
                        self._discovery = {"transport": "rest", "endpoint": self.rest_url, "fallback_reason": str(exc)}
            return dict(self._discovery)

    def _rpc(self, method, params, *, notification=False):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = uuid4().hex
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if method != "initialize":
            headers["MCP-Protocol-Version"] = PROTOCOL
            if self._session:
                headers["Mcp-Session-Id"] = self._session
        request = Request(self.mcp_url, json.dumps(message).encode(), headers)
        response_headers, body = _read(request, self.timeout)
        if notification and not body.strip():
            return {}
        if response_headers.get("Content-Type", "").startswith("text/event-stream"):
            frames = body.replace("\r\n", "\n").split("\n\n")
            candidates = []
            for frame in frames:
                data = "\n".join(line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:"))
                if data:
                    candidates.append(_json(data))
        else:
            candidates = [_json(body)]
        for rpc in candidates:
            if "error" in rpc:
                raise OLSError(f"MCP {method} reported a JSON-RPC error")
            if rpc.get("id") != message.get("id"):
                continue
            if rpc.get("jsonrpc") == "2.0" and isinstance(rpc.get("result"), dict):
                if method == "initialize":
                    self._session = response_headers.get("Mcp-Session-Id")
                return rpc["result"]
        raise OLSError(f"Invalid MCP reply to {method}")

    def _discover_mcp(self):
        reply = self._rpc("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "liverkg-ols-reference", "version": "1"},
        })
        if reply.get("protocolVersion") != PROTOCOL:
            raise OLSError("Unsupported MCP protocol version")
        self._rpc("notifications/initialized", {}, notification=True)
        catalog = self._rpc("tools/list", {})
        items = _array(catalog.get("tools"), "tools/list")
        tools = {item["name"]: item.get("inputSchema", {}) for item in items if isinstance(item, dict) and "name" in item}
        for name, params in REQUIRED_TOOLS.items():
            schema = tools.get(name, {})
            properties = schema.get("properties", {})
            if not params.issubset(properties) or set(schema.get("required", [])) - params:
                raise OLSError(f"Incompatible or missing OLS MCP tool {name}")
        return {"transport": "mcp", "endpoint": self.mcp_url, "protocol": PROTOCOL,
                "server_version": reply.get("serverInfo", {}).get("version"), "tools": sorted(tools)}

    def _get(self, url):
        _, body = _read(Request(url, headers={"Accept": "application/json"}), self.timeout)
        return _json(body)

    def _tool(self, name, arguments):
        response = self._rpc("tools/call", {"name": name, "arguments": arguments})
        content = _array(response.get("content"), "content")
        if response.get("isError"):
            raise OLSError(f"MCP {name} reported a tool error")
        if len(content) != 1 or content[0].get("type") != "text":
            raise OLSError(f"Unexpected MCP {name} content")
        return _json(content[0]["text"])

    def search_classes(self, ontology, query):
        """Discover classes in a caller-selected ontology through the shared OLS MCP."""
        if self.discover()["transport"] != "mcp":
            raise OLSError("OLS MCP search unavailable")
        page = self._tool("searchClasses", {
            "ontologyId": ontology, "query": query, "includeObsoleteEntities": False,
            "pageNum": 0, "pageSize": self.page_size,
        })
        return _array(page.get("items"), "items")[:self.page_size]
