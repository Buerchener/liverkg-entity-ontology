"""Verify the reference review gate and the direction of recovered placements."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from entity_ontology.hierarchy.entity_placement_dag import EntityDAG
from entity_ontology.hierarchy.entity_reference import EntityReferenceLookup, evidence_for_pair
from entity_ontology.hierarchy.go_placement import complete_go_details, verified_go_terms
from entity_ontology.hierarchy.placement import place_one


PARENT = {"id": "parent", "name": "Liver cancer", "definition": "A cancer of the liver.",
          "kind": "Disease", "aliases": [], "source_mentions": [], "origin": "literature_mention"}
CHILD = {**PARENT, "id": "child", "name": "Hepatocellular carcinoma",
         "definition": "A malignant neoplasm arising from hepatocytes."}


def prompt_fixture(stage, items):
    if stage == "incremental_propose":
        return [{"id": "child", "comparisons": [{"id": "parent", "a_in_b": None, "b_in_a": False,
                                                "reason": "uncertain forward direction"}], "new_parent": None}]
    return [{"id": item["id"], "supported": (False if item["child"]["id"] == "parent" else None),
             "reason": "independent fixture"} for item in items]


def source_fixture(child="MONDO:1", parent="MONDO:2", predicate="is_a"):
    terms = [{"concept_id": key, "term_id": value, "definition": "official candidate",
              "source_url": "https://example.org/term"}
             for key, value in [("child", "MONDO:1"), ("parent", "MONDO:2")]]
    edge = {"child_id": child, "parent_id": parent, "predicate": predicate,
            "source_url": "https://example.org/edge"}
    return {"status": "REFERENCE_ONLY", "source": "MONDO", "terms": terms, "relations": [edge]}


class ReferencePlacementTest(TestCase):
    def place(self, result):
        dag = EntityDAG(["Disease"])
        dag.place(PARENT)
        external = Mock(return_value=result)
        go = Mock()
        calls = []

        def request(task):
            stage, _, payload = task
            items = payload["items"]
            calls.append(stage)
            if stage == "incremental_reference_review":
                self.assertEqual([item["id"] for item in items], ["existing:parent:parent"])
                self.assertEqual(payload["entity_references"][0]["is_a_evidence"], result["relations"])
                return [{"id": items[0]["id"], "supported": True, "reason": "MONDO:1 → MONDO:2"}]
            if stage == "incremental_edge_review":
                self.assertEqual([(row["child"]["id"], row["parent"]["id"]) for row in items],
                                 [("child", "parent"), ("parent", "child")])
            return prompt_fixture(stage, items)

        decision = place_one(dag, CHILD, request=request, go_lookup=go, entity_lookup=external)
        go.assert_not_called()
        external.assert_called_once()
        return decision, calls

    def test_uncertain_proposal_can_be_resolved_by_sourced_direction(self):
        decision, calls = self.place(source_fixture())
        self.assertIn("incremental_reference_review", calls)
        self.assertEqual(decision["action"], "as_child")
        self.assertEqual(decision["parent_ids"], ["parent"])
        self.assertEqual(decision["entity_reference"]["review_status"], "reviewed")
        self.assertEqual(decision["reference_supported_edges"], [{"child_id": "child", "parent_id": "parent"}])

    def test_source_needs_direction_predicate_and_two_endpoints(self):
        missing_parent = source_fixture()
        missing_parent["terms"].pop()
        for fixture in [missing_parent, source_fixture("MONDO:2", "MONDO:1"), source_fixture(predicate="part_of"),
                        {"status": "LOOKUP_FAILED", "source": "MONDO", "terms": [], "relations": []}]:
            with self.subTest(fixture=fixture):
                decision, calls = self.place(fixture)
                self.assertNotIn("incremental_reference_review", calls)
                self.assertEqual(decision["parent_ids"], ["type:Disease"])
                self.assertEqual(decision["comparisons"][0]["result"], "needs_review")
                self.assertEqual(decision["reference_supported_edges"], [])

    def test_lookup_reuses_a_completed_record_and_routes_gene(self):
        with TemporaryDirectory() as directory:
            lookup = EntityReferenceLookup(Path(directory))
            lookup.ols = Mock()
            lookup.hgnc = Mock()
            lookup.hgnc.lookup.return_value = {"status": "NO_MATCH", "source": "HGNC"}
            gene = {**CHILD, "kind": "Gene"}
            self.assertEqual(lookup(gene, []), lookup(gene, []))
            lookup.hgnc.lookup.assert_called_once_with(gene, [])
            lookup.ols.lookup.assert_not_called()

    def test_go_reuses_observed_edge_and_checks_the_missing_endpoint(self):
        url = "https://www.ebi.ac.uk/ols4/api/ontologies/go/terms/example"
        first_id, second_id = "GO:0005741", "GO:0031966"
        target = {**CHILD, "kind": "CellularComponent", "go_id": first_id}
        parent = {**PARENT, "kind": "CellularComponent", "go_id": second_id}
        pair = {"id": "pair", "child": target, "parent": parent, "strict": True}
        child_term = {"go_id": first_id, "name": target["name"], "definition": target["definition"], "source": url}
        parent_term = {"go_id": second_id, "name": parent["name"], "definition": parent["definition"], "source": url}
        records = [
            {"query_id": "q3", "tool": "detail", "ok": True, "result": {"terms": [child_term]}},
            {"query_id": "q4", "tool": "children", "ok": True, "result": {
                "edges": [{"subject": first_id, "predicate": "is_a", "object": second_id, "source": url}],
            }},
        ]
        response = {"concept_id": "child", "status": "MATCHED", "go_id": first_id, "go_name": target["name"],
                    "go_definition": target["definition"], "relation_type": "equivalent", "source": url,
                    "ontology_relations": [], "term_query_id": "q3", "tool_calls": 4, "query_records": records}
        call = Mock(return_value={"terms": [parent_term]})
        self.assertIsNone(evidence_for_pair(pair, verified_go_terms(response, [pair])))
        complete = complete_go_details(response, [pair], call)
        call.assert_called_once_with("detail", {"go_id": second_id})
        proof = evidence_for_pair(pair, verified_go_terms(complete, [pair]))
        self.assertEqual(proof["is_a_evidence"], [
            {"child_id": first_id, "parent_id": second_id, "predicate": "is_a", "source_url": url},
        ])
