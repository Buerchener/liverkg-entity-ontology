"""Regressions for staging, re-alignment, and evidence-gated publication."""

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from unittest.mock import Mock
import unittest
from unittest.mock import patch

from entity_ontology.enrichment.pipeline import _case, _Models
from entity_ontology.enrichment.parent_fallback import review_relations
from entity_ontology.hierarchy.ranking_journal import ContractError
from entity_ontology.hierarchy.entity_placement_dag import EntityDAG


class _Model:
    def request(self, task, refs):
        stage, _, payload = task
        if stage == "incremental_propose":
            return [{"id": payload["items"][0]["id"], "comparisons": [], "new_parent": None}]
        if stage == "enrichment_hierarchy_review":
            return [{"id": item["id"], "supported": True, "reason": "Verified entity type."}
                    for item in payload["items"]]
        raise AssertionError(stage)


class EnrichmentPipelineTests(unittest.TestCase):
    def setUp(self):
        self.dag = EntityDAG(["Disease"])
        definition = "Description from a fixture source."
        self.kg = {
            "id": "4:db:1", "name": "Fixture condition", "kind": "Disease", "aliases": [],
            "standard_ids": ["MONDO:0010000"], "definition": definition,
            "source": {"database": "liver", "element_id": "4:db:1"},
            "definition_evidence": {"database": "fixture", "definition": definition},
        }

    def _run_case(self):
        with TemporaryDirectory() as directory:
            return _case(self.dag, self.kg, {}, Path(directory), _Model(), None, {}, use_ols=False)

    def test_commits_new_leaf_only_after_same_record_alignment_check(self):
        expanded, record = self._run_case()
        self.assertEqual(record["status"], "inserted")
        self.assertEqual(len(self.dag.nodes), 1)
        self.assertEqual(len(expanded.nodes), 2)
        mapping = record["mapping"]
        self.assertEqual(mapping["relation_type"], "MAPS_TO")
        self.assertIn((mapping["ontology_node_id"], "type:Disease"), expanded.edges)
        self.assertEqual(mapping["evidence"]["identity"]["source_anchor"]["element_id"], self.kg["id"])
        self.assertTrue(record["realignment"]["accepted"])
        self.assertEqual(record["decision_stage"], "mapped")

    def test_rejected_alignment_discards_verified_placement(self):
        with patch("entity_ontology.enrichment.pipeline.realign",
                   return_value={"accepted": False, "reason": "competing_exact_identity"}):
            expanded, record = self._run_case()
        self.assertIs(expanded, self.dag)
        self.assertEqual(record["placement"]["status"], "inserted")
        self.assertEqual(record["status"], "needs_review")
        self.assertEqual(record["reason"], "competing_exact_identity")
        self.assertEqual(record["decision_stage"], "identity_realignment")
        self.assertIsNone(record["mapping"])

    def test_targeted_parent_retry_receives_partial_rows_only_for_owned_stages(self):
        models = object.__new__(_Models)
        models.journal = Mock(prompts={}, settings={}, last_ref=None)
        models.lock, models.parameters = Lock(), {}
        payload = {"items": [{"id": "a"}, {"id": "b"}]}
        partial = [{"id": "b", "relation_type": "UNCERTAIN"}]
        models.journal.call.return_value = {"rows": partial}
        for stage in ("enrichment_parent_fallback_review", "enrichment_typed_relation_review"):
            self.assertEqual(models.request((stage, "prompt", payload), []), partial)
        with self.assertRaises(ContractError):
            models.request(("enrichment_hierarchy_review", "prompt", payload), [])

    def test_provider_single_item_then_alternate_envelope_repairs_only_missing_parent(self):
        models = object.__new__(_Models)
        models.journal = Mock(prompts={}, settings={}, last_ref=None)
        models.lock, models.parameters = Lock(), {}
        def row(ident):
            return {"id": ident, "relation_type": "IS_A", "same_endpoint_scope": True,
                    "informative": True, "reason": "Verified directed assertion."}
        models.journal.call.side_effect = [row("q"), {"items": [row("p")]}]
        trace = {}
        reviews = review_relations(lambda task: models.request(task, []),
                                   "enrichment_parent_fallback_review", [{"id": "p"}, {"id": "q"}], {},
                                   trace=trace)
        self.assertTrue(trace["complete"])
        self.assertEqual(set(reviews), {"p", "q"})
        self.assertEqual([r["requested_ids"] for r in trace["attempts"]], [["p", "q"], ["p"]])


if __name__ == "__main__":
    unittest.main()
