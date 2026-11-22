"""Bounded correction and failure behavior without model or network calls."""

from copy import deepcopy
import unittest

from entity_ontology.alignment.decisions import validate_assessments
from entity_ontology.alignment.pipeline import AlignmentModel
from entity_ontology.hierarchy.ranking_journal import BudgetExceeded, ContractError


def request_payload():
    return {
        "kg": {"id": "k", "name": "Concept", "definition": "The supplied definition of this concept."},
        "items": [{"id": "o", "name": "Concept", "definition": "The supplied definition of this concept."}],
    }


def answer(*, invalid_citation=False):
    return {"rows": [{
        "id": "o", "relation_type": "MAPS_TO", "confidence": "high",
        "kg_entity_level": "class", "ontology_entity_level": "class", "same_scope": True,
        "rationale": "The supplied definitions have the same meaning.",
        "evidence": [{"side": "kg", "field": "definition",
                      "quote": "Text appearing only in an external lookup." if invalid_citation
                      else "The supplied definition of this concept."}],
    }]}


class FakeJournal:
    def __init__(self, outcomes, previous_error=None):
        self.outcomes = list(outcomes)
        self.previous_error = previous_error
        self.calls, self.invalid = [], []
        self.last_ref = None

    def invalid_reason(self, stage, payload):
        return self.previous_error

    def call(self, stage, payload):
        self.calls.append((stage, deepcopy(payload)))
        value = self.outcomes.pop(0)
        # A budget refusal creates no journal entry; preserve existing ref like
        # LLMJournal does so tests catch accidental attribution to previous work.
        if isinstance(value, BudgetExceeded):
            raise value
        self.last_ref = "llm/call-%d.json" % len(self.calls)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    def mark_invalid(self, reason):
        self.invalid.append((self.last_ref, reason))


def model_with(journal):
    model = AlignmentModel.__new__(AlignmentModel)
    model.journal = journal
    return model


class AlignmentModelCorrectionTests(unittest.TestCase):
    def test_invalid_local_citation_gets_one_corrected_attempt(self):
        journal = FakeJournal([answer(invalid_citation=True), answer()])
        model, payload = model_with(journal), request_payload()
        result = model.assess(payload)
        self.assertEqual(result, answer()["rows"])
        self.assertEqual(len(journal.calls), 2)
        self.assertNotIn("output_format_correction", journal.calls[0][1])
        self.assertIn("LOCAL", journal.calls[1][1]["output_format_correction"])
        self.assertNotIn("output_format_correction", payload)
        self.assertEqual(len(journal.invalid), 1)
        self.assertEqual(model.last_call_refs, ["llm/call-1.json", "llm/call-2.json"])

    def test_second_invalid_response_stops_without_third_attempt(self):
        journal = FakeJournal([answer(invalid_citation=True), answer(invalid_citation=True)])
        with self.assertRaisesRegex(ContractError, "LOCAL"):
            model_with(journal).assess(request_payload())
        self.assertEqual(len(journal.calls), 2)
        self.assertEqual(len(journal.invalid), 2)

    def test_saved_invalid_original_uses_only_correction(self):
        journal = FakeJournal([answer()], previous_error="Previous invalid local citation")
        model = model_with(journal)
        self.assertEqual(model.assess(request_payload()), answer()["rows"])
        self.assertEqual(len(journal.calls), 1)
        self.assertIn("Previous invalid local citation", journal.calls[0][1]["output_format_correction"])
        self.assertFalse(journal.invalid)

    def test_saved_invalid_original_does_not_allow_two_corrections(self):
        journal = FakeJournal([answer(invalid_citation=True)], previous_error="Previous error")
        with self.assertRaises(ContractError):
            model_with(journal).assess(request_payload())
        self.assertEqual(len(journal.calls), 1)

    def test_initial_budget_failure_is_not_a_schema_retry(self):
        journal = FakeJournal([BudgetExceeded("maximum_llm_calls")])
        journal.last_ref = "llm/previous-entity.json"
        model = model_with(journal)
        with self.assertRaises(BudgetExceeded):
            model.assess(request_payload())
        self.assertEqual(len(journal.calls), 1)
        self.assertFalse(journal.invalid)
        self.assertFalse(model.last_call_refs)

    def test_correction_still_obeys_call_budget(self):
        journal = FakeJournal([answer(invalid_citation=True), BudgetExceeded("maximum_llm_calls")])
        model = model_with(journal)
        with self.assertRaises(BudgetExceeded):
            model.assess(request_payload())
        self.assertEqual(len(journal.calls), 2)
        self.assertEqual(len(journal.invalid), 1)
        self.assertEqual(model.last_call_refs, ["llm/call-1.json"])

    def test_bad_json_contract_error_can_be_corrected_and_remains_traceable(self):
        journal = FakeJournal([ContractError("Invalid JSON response"), answer()])
        model = model_with(journal)
        self.assertEqual(model.assess(request_payload()), answer()["rows"])
        self.assertEqual(len(journal.calls), 2)
        self.assertEqual(model.last_call_refs, ["llm/call-1.json", "llm/call-2.json"])

    def test_json_containers_in_scalar_fields_are_contract_failures(self):
        for field in ("relation_type", "confidence", "kg_entity_level", "ontology_entity_level"):
            for wrong in ([], {}):
                response = answer()
                response["rows"][0][field] = wrong
                with self.subTest(field=field, wrong=wrong), self.assertRaises(ContractError):
                    validate_assessments(response, ["o"])
        for field in ("side", "field"):
            response = answer()
            response["rows"][0]["evidence"][0][field] = []
            with self.subTest(citation_field=field), self.assertRaises(ContractError):
                validate_assessments(response, ["o"])


if __name__ == "__main__":
    unittest.main()
