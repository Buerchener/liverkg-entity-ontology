"""Route entity lookups and record external evidence separately from the local DAG."""

from ..storage import atomic_json, digest, read_json
from .go_placement import GO_TYPES
from .hgnc_reference import HGNCReference
from .ontology_ols import OntologyOLS


REFERENCE_VERSION = "typed-v1"


class EntityReferenceLookup:
    def __init__(self, directory):
        self.directory = directory
        self.ols = OntologyOLS()
        self.hgnc = HGNCReference()

    def __call__(self, target, questions):
        if target["kind"] in GO_TYPES:
            return None
        provider = self.hgnc if target["kind"] == "Gene" else self.ols
        request = {"version": REFERENCE_VERSION, "target": target, "questions": questions}
        path = self.directory / f"{digest(request)}.json"
        if path.exists():
            result = read_json(path)["result"]
            if result["status"] != "LOOKUP_FAILED":
                return result
        result = provider.lookup(target, questions)
        atomic_json(path, {"request": request, "result": result})
        return result


def evidence_for_pair(pair, result):
    """Limit review to source facts with both local endpoints represented."""
    terms = result.get("terms", [])
    child = [term for term in terms if term["concept_id"] == pair["child"]["id"]]
    parent = [term for term in terms if term["concept_id"] == pair["parent"]["id"]]
    if len(child) != 1 or len(parent) != 1:
        return None
    child_term, parent_term = child[0], parent[0]
    relations = [edge for edge in result.get("relations", []) if edge["predicate"] == "is_a"
                 and edge["child_id"] == child_term["term_id"] and edge["parent_id"] == parent_term["term_id"]]
    same_term = child_term["term_id"] == parent_term["term_id"]
    if not relations and (pair["strict"] or not same_term):
        return None
    return {"pair_id": pair["id"], "source": result["source"], "child_term": child_term,
            "parent_term": parent_term, "is_a_evidence": relations}
