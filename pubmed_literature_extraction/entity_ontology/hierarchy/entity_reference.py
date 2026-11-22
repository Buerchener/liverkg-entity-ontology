"""Cache OLS investigations and admit source-specific evidence to placement review."""

from ..storage import atomic_json, digest, read_json


REFERENCE_VERSION = "ols-agent-grounded-v3"


class EntityReferenceLookup:
    def __init__(self, directory, *, ols, settings=None):
        self.directory = directory
        self.ols = ols
        self.settings = settings

    def __call__(self, target, questions):
        request = {"version": REFERENCE_VERSION, "settings": self.settings, "target": target, "questions": questions}
        path = self.directory / f"{digest(request)}.json"
        if path.exists():
            result = read_json(path)["result"]
            if result["status"] != "LOOKUP_FAILED" and not result.get("retryable"):
                return result
        result = self.ols.lookup(target, questions)
        atomic_json(path, {"request": request, "result": result})
        return result


def evidence_for_pair(pair, result):
    """Limit review to source facts with both local endpoints represented."""
    for source in result.get("sources_checked", [result["source"]]):
        terms = [term for term in result.get("terms", []) if term.get("source", source) == source]
        relations = [edge for edge in result.get("relations", []) if edge.get("source", source) == source]
        proof = _source_evidence(pair, source, terms, relations)
        if proof is not None:
            return proof
    # HGNC represents a human gene locus as a named class with a named SO
    # superclass. Keep both issuing sources and the explicit child assertion.
    cross_edges = [edge for edge in result.get("relations", [])
                   if edge.get("source") == "HGNC" and edge.get("parent_source") == "SO"
                   and edge.get("source_url") and edge.get("predicate") == "is_a"]
    if cross_edges and pair["child"]["kind"] == pair["parent"]["kind"] == "Gene":
        terms = [term for term in result["terms"] if
                 (term.get("source"), term["concept_id"]) in {
                     ("HGNC", pair["child"]["id"]), ("SO", pair["parent"]["id"])}]
        terms = [term for term in terms if term["term_id"].startswith(term["source"] + ":")]
        proof = _source_evidence(pair, "HGNC → SO", terms, cross_edges)
        if proof is not None:
            return proof
    return None


def _source_evidence(pair, source, terms, edges):
    child = [term for term in terms if term["concept_id"] == pair["child"]["id"]]
    parent = [term for term in terms if term["concept_id"] == pair["parent"]["id"]]
    if len(child) != 1 or len(parent) != 1:
        return None
    child_term, parent_term = child[0], parent[0]
    relations = [edge for edge in edges if edge["predicate"] == "is_a"
                 and edge["child_id"] == child_term["term_id"] and edge["parent_id"] == parent_term["term_id"]]
    same_term = child_term["term_id"] == parent_term["term_id"]
    if not relations and (pair["strict"] or not same_term):
        return None
    return {"pair_id": pair["id"], "source": source, "child_term": child_term,
            "parent_term": parent_term, "is_a_evidence": relations}
