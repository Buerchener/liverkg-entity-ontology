"""GO-specific evidence tools used by the legacy reference agent."""

from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from ...hierarchy.ols_client import (
    EXACT_SYNONYM, IAO_DEFINITION, OBO, OBO_IN_OWL, OLSError,
    OLSClient as SharedOLSClient, _array, _values,
)


GO_NAMESPACE = OBO_IN_OWL + "hasOBONamespace"
GO_ASPECTS = {"biological_process", "molecular_function", "cellular_component"}
GO_ID = re.compile(r"GO:[0-9]{7}\Z")
SUBCLASS = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
PREDICATES = {
    SUBCLASS: "is_a",
    OBO + "BFO_0000050": "part_of",
    OBO + "BFO_0000051": "has_part",
    OBO + "RO_0002211": "regulates",
    OBO + "RO_0002212": "negatively_regulates",
    OBO + "RO_0002213": "positively_regulates",
}


def _iri(go_id):
    return OBO + go_id.replace(":", "_")


def _id_from_iri(value):
    if isinstance(value, str) and value.startswith(OBO):
        go_id = value[len(OBO):].replace("_", ":")
        if GO_ID.fullmatch(go_id) and value == _iri(go_id):
            return go_id
    return None


class OLSClient(SharedOLSClient):
    """Keep the original GO tool interface for historical workflows."""

    def _term_url(self, go_id, *, graph=False):
        encoded = quote(quote(_iri(go_id), safe=""), safe="")
        if graph:
            return f"{self.rest_url}/ontologies/go/terms/{encoded}/graph"
        return f"{self.rest_url}/v2/ontologies/go/classes/{encoded}"

    def call(self, name, args):
        """Run one logical tool and return GO evidence plus its physical requests."""
        if name not in {"search", "detail", "ancestors", "children", "relations"}:
            raise ValueError("Unknown GO lookup tool")
        param = "query" if name == "search" else "go_id"
        if not isinstance(args, dict) or set(args) != {param} or not isinstance(args[param], str):
            raise ValueError(f"{name} requires exactly one string argument: {param}")
        value = args[param].strip()
        if name == "search":
            if not value or len(value) > 500:
                raise ValueError("query must have 1–500 characters")
        elif not GO_ID.fullmatch(value):
            raise ValueError("go_id must be GO: followed by seven digits")
        result = {"terms": [], "edges": [], "transport": "rest", "requests": []}
        if name in {"detail", "relations"}:
            # An explicit MCP mode requires a compatible server even for the two
            # records that only the REST API exposes in a verifiable form.
            if self.mode == "mcp":
                self.discover()
            if name == "detail":
                self._detail(value, result)
            else:
                self._relations(value, result)
        else:
            info = self.discover()
            result["transport"] = info["transport"]
            if "fallback_reason" in info:
                result["fallback_reason"] = info["fallback_reason"]
            if info["transport"] == "mcp":
                self._mcp_page(name, value, result)
            else:
                self._rest_page(name, value, result)
        return result

    def _detail(self, go_id, result):
        obj, url = self._active_class(go_id, result)
        labels = _values(obj.get("label"))
        definitions = _values(obj.get(IAO_DEFINITION))
        if not labels:
            raise OLSError("GO class has no label")
        namespaces = _values(obj.get(GO_NAMESPACE))
        namespace = namespaces[0] if len(namespaces) == 1 and namespaces[0] in GO_ASPECTS else None
        result["terms"] = [{"go_id": go_id, "name": labels[0], "definition": definitions[0] if definitions else None,
                            "namespace": namespace, "exact_synonyms": _values(obj.get(EXACT_SYNONYM)), "source": url}]

    def _active_class(self, go_id, result):
        url = self._term_url(go_id)
        obj = self._get(url)
        if (obj.get("curie") != go_id or obj.get("iri") != _iri(go_id) or obj.get("ontologyId") != "go"
                or obj.get("isObsolete") is not False or obj.get("isDefiningOntology") is not True
                or not isinstance(obj.get("type"), list) or "class" not in obj["type"]):
            raise OLSError("OLS did not confirm an active, GO-defined class with the requested ID")
        result["requests"].append({"transport": "rest", "url": url})
        return obj, url

    def _mcp_page(self, name, value, result):
        args = {"ontologyId": "go", "pageNum": 0, "pageSize": self.page_size}
        if name == "search":
            tool = "searchClasses"
            args.update(query=value, includeObsoleteEntities=False)
        else:
            tool = {"ancestors": "getAncestors", "children": "getChildren"}[name]
            args["classIri"] = _iri(value)
        page = self._tool(tool, args)
        rows = _array(page.get("items"), "items")
        result["requests"].append({"transport": "mcp", "endpoint": self.mcp_url, "tool": tool, "arguments": args})
        self._page_terms(rows, name, value, result)
        result["total_before_go_filter"] = page.get("totalElements")
        result["page_size"] = self.page_size

    def _rest_page(self, name, value, result):
        if name == "search":
            params = {"ontology": "go", "type": "class", "local": "true", "obsoletes": "false",
                      "rows": self.page_size, "q": value}
            url = f"{self.rest_url}/search?{urlencode(params)}"
            data = self._get(url).get("response", {})
            rows = _array(data.get("docs"), "response.docs")
            total = data.get("numFound")
        else:
            params = {"id": value, "page": 0, "size": self.page_size}
            url = f"{self.rest_url}/ontologies/go/{name}?{urlencode(params)}"
            data = self._get(url)
            total = data.get("page", {}).get("totalElements")
            rows = data.get("_embedded", {}).get("terms")
            if rows is None and total == 0:
                rows = []
            rows = _array(rows, "_embedded.terms")
        result["requests"].append({"transport": "rest", "url": url})
        self._page_terms(rows, name, value, result)
        result["total_before_go_filter"] = total
        result["page_size"] = self.page_size

    def _page_terms(self, rows, name, focus, result):
        mcp = result["transport"] == "mcp"
        if name != "search" and not rows:
            # OLS also returns an empty page for an unknown ID; distinguish a true leaf.
            self._active_class(focus, result)
        verified_children = set()
        if rows and name == "children" and not mcp:
            graph_rows, graph_url = self._graph(focus, result)
            verified_children = {_id_from_iri(edge.get("source")) for edge in graph_rows
                                 if edge.get("uri") == SUBCLASS and edge.get("target") == _iri(focus)}
        for obj in rows[:self.page_size]:
            go_id = _id_from_iri(obj.get("iri"))
            supplied_id = obj.get("curie") if mcp else obj.get("obo_id")
            ontology = obj.get("ontologyId") if mcp else obj.get("ontology_name")
            if not go_id or ontology != "go":
                continue  # GO imports BFO, RO and other non-GO classes.
            if supplied_id != go_id:
                raise OLSError("Inconsistent GO ID and IRI in OLS page")
            if mcp:
                if "class" not in obj.get("type", []) or obj.get("isObsolete") is not False:
                    continue
            elif obj.get("is_obsolete") or (name == "search" and obj.get("type") != "class"):
                continue
            labels = _values(obj.get("label"))
            if not labels:
                raise OLSError("Missing GO label in OLS page")
            source = self._term_url(go_id)
            result["terms"].append({"go_id": go_id, "name": labels[0], "definition": None, "source": source})
            if name != "children":
                continue
            if mcp:
                parents = _array(obj.get("directParent"), "directParent")
                if not any(isinstance(p, dict) and _id_from_iri(p.get("iri")) == focus for p in parents):
                    continue
            elif go_id not in verified_children:
                continue
            result["edges"].append({"subject": go_id, "predicate": "is_a", "object": focus, "predicate_iri": SUBCLASS,
                                    "source": source if mcp else graph_url})

    def _graph(self, go_id, result):
        url = self._term_url(go_id, graph=True)
        graph = self._get(url)
        rows = _array(graph.get("edges"), "edges")
        nodes = _array(graph.get("nodes"), "nodes")
        result["requests"].append({"transport": "rest", "url": url})
        if not any(node.get("iri") == _iri(go_id) for node in nodes):
            if nodes or rows:
                raise OLSError("Requested GO class missing from OLS graph")
            self._active_class(go_id, result)
        return rows, url

    def _relations(self, go_id, result):
        rows, url = self._graph(go_id, result)
        seen = set()
        for edge in rows:
            if edge.get("source") != _iri(go_id):
                continue
            target = _id_from_iri(edge.get("target"))
            predicate = PREDICATES.get(edge.get("uri"))
            if not target or not predicate or (predicate, target) in seen:
                continue
            seen.add((predicate, target))
            result["edges"].append({"subject": go_id, "predicate": predicate, "object": target,
                                    "predicate_iri": edge["uri"], "source": url})
        result["total_supported_outgoing"] = len(result["edges"])
        result["edges"] = result["edges"][:self.page_size]
