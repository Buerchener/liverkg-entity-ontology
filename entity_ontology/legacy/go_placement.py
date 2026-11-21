"""Read-only GO lookup for open placement questions."""

import json
import subprocess
import sys

from ..storage import atomic_json, digest, norm, read_json
from .go_reference.ols import GO_ID, OLSClient, OLSError
from .go_reference.schema import direct_edges


GO_TYPES = {"Pathway", "BiologicalProcess", "MolecularFunction", "CellularComponent"}
STATUSES = {"MATCHED", "REFERENCE_ONLY", "NO_MATCH", "UNCERTAIN", "LOOKUP_FAILED"}
MAX_TOOL_CALLS = 5
AGENT_TOOL_CALLS = 4


def reference_evidence(result):
    if result["status"] not in {"MATCHED", "REFERENCE_ONLY"}:
        return None
    if not (result.get("go_definition") or result.get("ontology_relations")):
        return None
    return {key: result[key] for key in (
        "concept_id", "status", "go_id", "go_name", "go_definition",
        "relation_type", "source", "ontology_relations", "term_query_id",
    )}


def go_query_terms(queries, *, detailed=False):
    return {term["go_id"]: term for query in queries
            if query.get("ok") is True and (not detailed or query["tool"] == "detail")
            for term in query["result"].get("terms", []) if not detailed or term.get("definition")}


def matching_go_ids(concept, official):
    chosen = concept.get("go_id")
    if chosen:
        return [chosen] if GO_ID.fullmatch(chosen) else []
    labels = {norm(label) for label in [concept["name"], *concept["aliases"]]}
    return [term_id for term_id, term in official.items()
            if any(norm(label) in labels for label in [term["name"], *term.get("exact_synonyms", [])])]


def verified_go_terms(result, questions):
    """Map both local endpoints to independently retrieved GO details."""
    queries = [*result.get("query_records", []), *result.get("placement_queries", [])]
    official = go_query_terms(queries, detailed=True)
    endpoints = {item[side]["id"]: item[side] for item in questions for side in ("child", "parent")}
    terms = [{"concept_id": ident, "term_id": term_id, "name": official[term_id]["name"],
              "definition": official[term_id]["definition"], "source_url": official[term_id]["source"]}
             for ident, concept in endpoints.items() for term_id in matching_go_ids(concept, official)
             if term_id in official]
    observed = [*result.get("ontology_relations", []), *direct_edges(result["go_id"], queries)]
    edges = {(row["subject"], row["predicate"], row["object"]): row for row in observed}
    return {"source": "GO", "terms": terms, "relations": [
        {"child_id": edge["subject"], "parent_id": edge["object"], "predicate": edge["predicate"],
         "source_url": edge["source"]} for edge in edges.values()
    ]}


def complete_go_details(result, questions, call):
    """Use the remaining GO budget to verify known term pairs directly."""
    if reference_evidence(result) is None:
        return result
    queries = result.get("query_records", [])
    detailed = go_query_terms(queries, detailed=True)
    known = go_query_terms(queries) | detailed
    edges = {(edge["subject"], edge["object"]) for edge in direct_edges(result["go_id"], queries)
             if edge["predicate"] == "is_a"}
    checked_children = {row["arguments"]["go_id"] for row in queries
                        if row.get("tool") == "relations" and row.get("ok") is True}
    remaining = MAX_TOOL_CALLS - result["tool_calls"]
    placement_queries = []

    def observe(tool, term_id):
        if len(placement_queries) >= remaining:
            return None
        index = result["tool_calls"] + len(placement_queries) + 1
        record = {"query_id": f"q{index}", "tool": tool, "arguments": {"go_id": term_id}}
        try:
            observation = call(tool, record["arguments"])
        except (OLSError, ValueError):
            placement_queries.append({**record, "ok": False, "error": "GO evidence lookup unavailable"})
            return None
        placement_queries.append({**record, "ok": True, "result": observation})
        return observation

    for pair in questions:
        if len(placement_queries) >= remaining:
            break
        child = matching_go_ids(pair["child"], known)
        parent = matching_go_ids(pair["parent"], known)
        if len(child) != 1 or len(parent) != 1:
            continue
        edge = child[0], parent[0]
        if child != parent and edge not in edges and child[0] not in checked_children:
            checked_children.add(child[0])
            observe("relations", child[0])
            edges.update((row["subject"], row["object"]) for row in direct_edges(result["go_id"], placement_queries)
                         if row["predicate"] == "is_a")
        if edge in edges or (child == parent and not pair["strict"]):
            for term_id in dict.fromkeys([*child, *parent]):
                if term_id not in detailed:
                    observe("detail", term_id)
                    detailed.update(go_query_terms(placement_queries, detailed=True))
    return {**result, "placement_queries": placement_queries} if placement_queries else result


class GOReferenceLookup:
    def __init__(self, config, directory):
        self.config = config.resolve()
        self.directory = directory.resolve()
        self.ols = OLSClient(mode="rest", timeout=5)

    def __call__(self, concept, questions):
        concept_id = concept["id"]
        parents = {
            item["parent"]["id"]: item["parent"]
            for item in questions
            if item["child"]["id"] == concept_id
        }
        request = {
            "concept_id": concept_id,
            "name": concept["name"],
            "definition": concept["definition"],
            "candidate_parents": [
                {"concept_id": ident, "name": row["name"], "definition": row["definition"],
                 **({"go_id": row["go_id"]} if row.get("go_id") else {})}
                for ident, row in list(parents.items())[:50]
            ],
            "taxonomy_context": {
                "entity_type": concept["kind"],
                "target_go_id": concept.get("go_id"),
                "evidence_request": (
                    "What direct GO relation, if any, connects the corresponding terms? "
                    "Preserve the observed predicate and direction. Cite the detail query "
                    "for the GO term selected as the reference to this concept."
                ),
                "unresolved_parent_child_questions": [
                    {"child": item["child"]["name"], "parent": item["parent"]["name"],
                     "child_definition": item["child"]["definition"],
                     "parent_definition": item["parent"]["definition"],
                     "child_id": item["child"]["id"], "parent_id": item["parent"]["id"]}
                    for item in questions
                ],
            },
        }
        path = self.directory / f"{digest(concept_id)}.json"
        request_id = digest(request)
        if path.exists():
            entry = read_json(path)
            if entry["request_sha256"] != request_id:
                return self.failed(concept_id, "lookup_already_used_for_another_context")
            # The checkpoint records a started call even if the process stopped mid-run.
            return entry.get("result") or self.failed(concept_id, "interrupted_lookup")

        entry = {"request_sha256": request_id, "request": request}
        atomic_json(path, entry)
        result = complete_go_details(self._lookup(request), questions, self.ols.call)
        atomic_json(path, {**entry, "result": result})
        return result

    @staticmethod
    def failed(concept_id, reason):
        return {"concept_id": concept_id, "status": "LOOKUP_FAILED", "exit_reason": reason}

    def _lookup(self, request):
        concept_id = request["concept_id"]
        command = [
            sys.executable, "-m", "entity_ontology.legacy.go_reference",
            "--model-config", str(self.config), "--mode", "auto",
            "--max-tool-calls", str(AGENT_TOOL_CALLS), "--model-timeout", "20",
            "--state-dir", str(self.directory / "agent-runs"),
        ]
        try:
            reply = subprocess.run(
                command, input=json.dumps(request, ensure_ascii=False),
                capture_output=True, text=True, timeout=240,
            )
        except subprocess.TimeoutExpired:
            return self.failed(concept_id, "reference_timeout")
        except (OSError, UnicodeError):
            return self.failed(concept_id, "reference_runtime_unavailable")
        if reply.returncode:
            return self.failed(concept_id, f"reference_exit_{reply.returncode}")
        try:
            result = json.loads(reply.stdout)
            if (result["concept_id"] != concept_id or result["status"] not in STATUSES
                    or type(result["tool_calls"]) is not int
                    or not 0 <= result["tool_calls"] <= MAX_TOOL_CALLS):
                raise ValueError("Inconsistent GO reference result")
            # The standalone Agent has already validated the citation against OLS evidence.
            reference_evidence(result)
            return result
        except (KeyError, TypeError, ValueError):
            return self.failed(concept_id, "invalid_reference_output")
