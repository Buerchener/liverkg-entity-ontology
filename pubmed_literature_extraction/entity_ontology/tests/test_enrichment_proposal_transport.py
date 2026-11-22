"""A complete target proposal can coexist with irrelevant top-level rows."""
from threading import Lock
import unittest
from unittest.mock import Mock

from entity_ontology.enrichment.pipeline import _Models
from entity_ontology.hierarchy.placement import _sourced_proposal_check
from entity_ontology.hierarchy.ranking_journal import ContractError


class EnrichmentProposalTransportTests(unittest.TestCase):
    def setUp(self):
        self.models = object.__new__(_Models)
        self.models.journal = Mock(prompts={}, settings={}, last_ref=None)
        self.models.lock, self.models.parameters = Lock(), {}
        self.payload = {"items": [{"id": "target", "candidates": [{"id": "candidate"}]}]}
        self.row = {"id": "target", "comparisons": [
            {"id": "candidate", "a_in_b": False, "b_in_a": True, "reason": "Definition comparison."}
        ], "new_parent": None}

    def call(self, rows, stage="incremental_propose"):
        self.models.journal.call.return_value = {"rows": rows}
        selected = self.models.request((stage, "prompt", self.payload), [])
        return _sourced_proposal_check("target", {"candidate"})(selected)

    def test_unique_target_values_survive_without_using_other_top_level_judgments(self):
        foreign = {"id": "other-target", "comparisons": [
            {"id": "candidate", "a_in_b": True, "b_in_a": False, "reason": "Other direction."}
        ], "new_parent": {"name": "alien", "definition": "does not apply", "reason": "different target"}}
        parsed, parent = self.call([foreign, self.row])
        self.assertIsNone(parent)
        self.assertEqual(parsed["candidate"]["relation"], "broader")
        with self.assertRaises(ContractError):
            self.call([foreign, self.row], stage="enrichment_hierarchy_review")

    def test_missing_duplicate_or_unaddressable_rows_cannot_claim_unique_target(self):
        foreign = {**self.row, "id": "another"}
        for rows in ([foreign], [self.row, self.row], [self.row, foreign, foreign],
                     [self.row, {}], [self.row, {"id": " "}], [self.row, 1]):
            with self.subTest(rows=rows), self.assertRaises(ContractError):
                self.call(rows)

    def test_structural_or_candidate_errors_in_requested_row_still_fail(self):
        other = {**self.row, "id": "other"}
        for updates in ({"comparisons": None}, {"comparisons": []}, {"new_parent": "unverified"},
                        {"comparisons": [{**self.row["comparisons"][0], "a_in_b": "true"}]}):
            with self.subTest(updates=updates), self.assertRaises((ContractError, ValueError)):
                self.call([other, {**self.row, **updates}])

    def test_incomplete_document_is_not_reconstructed(self):
        self.models.journal.call.side_effect = ContractError("Incomplete or non-stop LLM response: length")
        with self.assertRaisesRegex(ContractError, "non-stop"):
            self.models.request(("incremental_propose", "prompt", self.payload), [])
