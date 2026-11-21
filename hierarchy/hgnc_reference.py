"""Use HGNC human gene records as identity evidence for local gene concepts."""

import json
import re
from threading import Lock
from time import monotonic, sleep
from urllib.parse import quote
from urllib.request import Request, urlopen


HGNC_REST = "https://rest.genenames.org"
HGNC_REPORT = "https://www.genenames.org/data/gene-symbol-report/#!/hgnc_id/"
REQUEST_BUDGET = 8
MAX_REPLY_BYTES = 524_288
SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,39}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,+()'/-]{0,119}")
HGNC_ID = re.compile(r"HGNC:[1-9][0-9]*")
HUMAN_PREFIX = re.compile(r"^(?:human|homo sapiens)\s+", re.I)
NONHUMAN_PREFIX = re.compile(
    r"^(?:non[- ]human|mouse|murine|mus musculus|rat|rattus norvegicus|"
    r"zebrafish|danio rerio|drosophila)\s+", re.I
)


def _labels(concept):
    for label in (concept["name"], *concept.get("aliases", [])[:2]):
        label = HUMAN_PREFIX.sub("", " ".join(label.split()))
        label = re.sub(r"\s+gene$", "", label, flags=re.I)
        if NAME.fullmatch(label):
            yield label


def _approved_candidates(rows, field, label):
    matching = {}
    for row in rows:
        term_id = row.get("hgnc_id", "")
        name, symbol = row.get("name"), row.get("symbol")
        values = row.get(field, [])
        values = [values] if isinstance(values, str) else values
        if (row.get("status") != "Approved" or not isinstance(term_id, str)
                or not HGNC_ID.fullmatch(term_id) or not isinstance(name, str) or not name
                or not isinstance(symbol, str) or not symbol or not isinstance(values, list)):
            continue
        if any(isinstance(value, str) and value.casefold() == label.casefold() for value in values):
            matching[term_id] = row
    return matching


class HGNCReference:
    def __init__(self, *, get_json=None, timeout=6):
        self.get_json = get_json or self._get_json
        self.timeout = timeout
        self._pace = Lock()
        self._next_request = 0.0

    def _get_json(self, url):
        with self._pace:
            sleep(max(0.0, self._next_request - monotonic()))
            self._next_request = monotonic() + 0.11
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=self.timeout) as response:
            data = response.read(MAX_REPLY_BYTES + 1)
        if len(data) > MAX_REPLY_BYTES:
            raise ValueError("Oversized HGNC response.")
        return json.loads(data)

    def lookup(self, concept, questions):
        requests, terms, replies, issues = [], [], {}, []

        def fetch(field, label):
            value = f'"{label}"' if " " in label else label
            url = f"{HGNC_REST}/fetch/{field}/{quote(value, safe='')}"
            if url not in replies:
                if len(requests) >= REQUEST_BUDGET:
                    issues.append("budget_exhausted")
                    return {}, url
                try:
                    body = self.get_json(url)["response"]
                    rows = body["docs"]
                    if (not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows)
                            or type(body["numFound"]) is not int or body["numFound"] != len(rows)):
                        raise ValueError("Incomplete or invalid HGNC records.")
                    replies[url] = _approved_candidates(rows, field, label)
                except (OSError, ValueError, KeyError, TypeError):
                    requests.append({"url": url, "status": "failed"})
                    replies[url] = {}
                    issues.append("lookup_failed")
                else:
                    requests.append({"url": url, "status": "ok", "exact_hits": len(replies[url])})
            return replies[url], url

        endpoints = {concept["id"]: concept}
        for item in questions:
            for endpoint in (item["child"], item["parent"]):
                endpoints.setdefault(endpoint["id"], endpoint)
        for endpoint in endpoints.values():
            if (endpoint.get("kind") != "Gene" or NONHUMAN_PREFIX.match(endpoint["name"])
                    or str(endpoint.get("taxon_id", "9606")).removeprefix("NCBITaxon:") != "9606"):
                issues.append("outside_human_gene_scope")
                continue
            term, ambiguous = self._lookup_endpoint(endpoint, fetch)
            if term:
                terms.append(term)
            elif ambiguous:
                issues.append("ambiguous_gene_reference")
        if terms:
            status = "REFERENCE_ONLY"
            reason = "HGNC matched a human gene name or symbol. Use the record for identity evidence."
        elif "lookup_failed" in issues:
            status, reason = "LOOKUP_FAILED", "HGNC did not provide complete lookup evidence."
        elif {"budget_exhausted", "ambiguous_gene_reference"}.intersection(issues) or not requests:
            status, reason = "UNCERTAIN", "No unique human gene reference was established."
        else:
            status, reason = "NO_MATCH", "The attempted HGNC lookups found no exact approved gene name or symbol."
        return {
            "status": status, "source": "HGNC", "terms": terms, "relations": [],
            "reason": reason, "issues": sorted(set(issues)), "requests": requests,
        }

    def _lookup_endpoint(self, endpoint, fetch):
        for label in dict.fromkeys(_labels(endpoint)):
            fields = ("symbol", "alias_symbol", "name") if SYMBOL.fullmatch(label) else ("name", "alias_name")
            for field in fields:
                candidates, url = fetch(field, label)
                if len(candidates) > 1:
                    return None, True
                if candidates:
                    term_id, row = next(iter(candidates.items()))
                    return {
                        "concept_id": endpoint["id"], "term_id": term_id,
                        "name": row["name"], "symbol": row["symbol"], "definition": None,
                        "organism": "Homo sapiens", "locus_type": row.get("locus_type"),
                        "matched_name": label, "matched_field": field,
                        "source_url": HGNC_REPORT + term_id, "retrieval_url": url,
                    }, False
        return None, False
