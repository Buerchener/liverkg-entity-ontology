"""Contract, export, and failure tests without paid services or a database."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from entity_ontology.alignment.pipeline import run_alignment
from entity_ontology.hierarchy.ranking_journal import BudgetExceeded


def node(ident, name="Concept"):
    return {"id": ident, "name": name, "kind": "Disease", "aliases": [], "standard_ids": [],
            "definition": "", "parent_ids": [], "is_root": False, "entity_level": "unknown"}


class AlignmentPipelineTests(unittest.TestCase):
    def test_no_llm_does_not_turn_name_match_into_alignment(self):
        with TemporaryDirectory() as directory:
            result = run_alignment({"nodes": [node("o")]}, {"nodes": [node("k")]}, directory, use_ols=False)
            self.assertEqual(result["REVIEW_REQUIRED"], 1)
            rows = json.loads((Path(directory) / "alignment.json").read_text())["rows"]
            self.assertIsNone(rows[0]["ontology_node_id"])
            self.assertEqual(rows[0]["relation_type"], "UNMATCHED")
            self.assertTrue((Path(directory) / "alignment.csv").exists())

    def test_budget_failure_is_review_and_all_entities_exported(self):
        class Model:
            settings = {}
            journal = SimpleNamespace(last_ref=None)

            def assess(self, payload):
                raise BudgetExceeded("limit")

        with TemporaryDirectory() as directory:
            result = run_alignment({"nodes": [node("o")]}, {"nodes": [node("k"), node("k2")]},
                                   directory, model=Model(), use_ols=False)
            self.assertEqual(result["total_kg_entities"], 2)
            self.assertEqual(result["REVIEW_REQUIRED"], 2)
            self.assertEqual(result["UNMATCHED"], 0)

    def test_output_refuses_changed_inputs_and_supports_repeat(self):
        with TemporaryDirectory() as directory:
            args = ({"nodes": [node("o")]}, {"nodes": [node("k")]}, directory)
            first = run_alignment(*args, use_ols=False)
            self.assertEqual(first, run_alignment(*args, use_ols=False))
            with self.assertRaisesRegex(ValueError, "different inputs"):
                run_alignment(args[0], {"nodes": [node("different")]}, directory, use_ols=False)

    def test_no_candidate_is_unmatched_with_bounded_scope(self):
        with TemporaryDirectory() as directory:
            result = run_alignment({"nodes": [node("o", "Alpha")]}, {"nodes": [node("k", "Zebra")]},
                                   directory, use_ols=False)
            self.assertEqual(result["UNMATCHED"], 1)
            self.assertIn("not an estimate", result["scope"])


if __name__ == "__main__":
    unittest.main()
