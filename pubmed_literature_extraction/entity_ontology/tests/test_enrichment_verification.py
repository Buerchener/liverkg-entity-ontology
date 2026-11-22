"""Verification and re-alignment regressions without network services."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from entity_ontology.enrichment.verification import realign, verify_concept


def node(ident, name, definition, *, kind="Disease", standard_ids=()):
    return {
        "id": ident, "name": name, "kind": kind, "definition": definition,
        "standard_ids": list(standard_ids), "aliases": [], "parent_ids": [],
        "is_root": False, "entity_level": "unknown", "taxon_id": None,
    }


class AlignmentFixture:
    settings = {}

    def __init__(self, relations):
        self.relations = relations
        self.journal = SimpleNamespace(last_ref=None)

    def assess(self, payload):
        rows = []
        for candidate in payload["items"]:
            relation = self.relations[candidate["id"]]
            rows.append({
                "id": candidate["id"], "relation_type": relation, "confidence": "high",
                "kg_entity_level": "unknown", "ontology_entity_level": "unknown",
                "same_scope": relation == "MAPS_TO", "rationale": "Fixture assessment.",
                "evidence": [
                    {"side": "kg", "field": "name", "quote": payload["kg"]["name"]},
                    {"side": "ontology", "field": "name", "quote": candidate["name"]},
                ],
            })
        return rows


class EnrichmentVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "alignment"
        self.kg = node("4:fixture:1", "Acute sample", "Documented narrower scope among the disease records.",
                       standard_ids=["MONDO:0001234"])
        self.kg["source"] = {"element_id": self.kg["id"], "database": "liver-kg-core-v02"}
        self.inserted = {
            **self.kg, "id": "enrichment:fixture",
            "enrichment_provenance": {
                "kg_source": self.kg["source"], "definition_evidence": [{"source": "fixture record"}],
            },
        }

    def test_supported_class_subsumption_can_continue_to_placement(self):
        broader = node("base", "Sample disease", "Documented general scope among the disease records.")
        result = verify_concept(self.kg, [broader], output=self.output,
                                model=AlignmentFixture({"base": "CLASS_SUBSUMPTION"}))
        self.assertEqual(result["action"], "new")
        self.assertEqual(result["alignment"]["decision_status"], "REVIEW_REQUIRED")

    def test_identity_in_existing_graph_uses_real_alignment_decision(self):
        result = verify_concept(self.kg, [self.inserted], output=self.output,
                                model=AlignmentFixture({self.inserted["id"]: "MAPS_TO"}))
        self.assertEqual(result["action"], "existing")
        self.assertEqual(result["ontology_node_id"], self.inserted["id"])
        self.assertEqual(result["alignment"]["decision_status"], "ALIGNED")

    def test_name_only_and_missing_definition_require_review(self):
        kg = {**self.kg, "definition": "", "standard_ids": []}
        candidate = node("homonym", kg["name"], "This definition denotes another concept.")
        result = verify_concept(kg, [candidate], output=self.output)
        self.assertEqual(result["action"], "needs_review")
        self.assertEqual(result["identity_evidence"]["unresolved_exact_matches"], [candidate["id"]])

    def test_empty_candidate_set_cannot_replace_missing_definition(self):
        kg = {**self.kg, "definition": ""}
        result = verify_concept(kg, [], output=self.output)
        self.assertEqual(result["reason"], "source_definition_missing")

    def test_ambiguous_nonexact_neighbour_needs_bound_identity_and_three_valid_citations(self):
        uri = "https://reference.example/record"
        mention = "Acute sample was observed in patients with this disorder."
        kg = {**self.kg, "reference_terms": [{
            "source": "MONDO", "term_id": "MONDO:0001234", "name": self.kg["name"], "source_url": uri,
            "definition": self.kg["definition"], "identity_evidence": {"record_id": "MONDO:0001234", "url": uri},
        }], "source_context": {"source": "PubMed:55", "literature_context": {
            "url": "https://pubmed.ncbi.nlm.nih.gov/55/", "pmid": "55",
            "mention_excerpts": [mention], "artifact": "local-paper-snapshot"}}}
        other = node("neighbour", "Sample disorder", "A family of disease states involving an unrelated attribute.")
        valid = {"id": "neighbour", "verdict": "DISTINCT", "confidence": "high", "kg_quote": kg["definition"],
                 "candidate_quote": other["definition"], "mention_quote": mention,
                 "reason": "The occurrence and the broader disease group have different defining criteria."}
        for idx, (source, response, action, calls) in enumerate([
            (kg, valid, "new", 1),
            (kg, {**valid, "mention_quote": "An invented citation that is sufficiently long."}, "needs_review", 1),
            (kg, {**valid, "confidence": "low"}, "needs_review", 1),
            ({**kg, "standard_ids": []}, valid, "needs_review", 0),
            ({**kg, "source_context": {}}, valid, "needs_review", 0),
            ({**kg, "aliases": [other["name"]]}, valid, "needs_review", 0),
        ]):
            with self.subTest(case=idx):
                reviewer = Mock(return_value=[response])
                result = verify_concept(source, [other], output=self.output / str(idx),
                                        model=AlignmentFixture({"neighbour": "UNCERTAIN"}), scope_request=reviewer)
                self.assertEqual(result["action"], action)
                self.assertIsNone(result["ontology_node_id"])
                self.assertEqual(reviewer.call_count, calls)

    def test_retrieval_only_realign_preserves_direct_source_identity(self):
        different_type = node("gene", self.kg["name"], "A distinct gene.", kind="Gene")
        result = realign(self.kg, [self.inserted, different_type], self.inserted["id"],
                         output=self.output, require_source_anchor=True)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "preserved_kg_record_identity")
        self.assertFalse(result["identity_evidence"]["alignment_model_used"])
        self.assertEqual(result["alignment"]["decision_status"], "REVIEW_REQUIRED")

    def test_inserted_definition_must_match_sourced_record_even_with_positive_model(self):
        edited = {**self.inserted, "definition": "Altered source meaning points to another concept."}
        result = realign(self.kg, [edited], edited["id"], output=self.output, require_source_anchor=True,
                         model=AlignmentFixture({edited["id"]: "MAPS_TO"}))
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "enriched_concept_source_anchor_mismatch")

    def test_other_kg_record_can_map_independently_to_existing_enriched_concept(self):
        other = {**self.kg, "id": "4:fixture:2",
                 "source": {**self.kg["source"], "element_id": "4:fixture:2"}}
        result = realign(other, [self.inserted], self.inserted["id"], output=self.output,
                         model=AlignmentFixture({self.inserted["id"]: "MAPS_TO"}))
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "shared_standard_id")
        self.assertNotIn("source_anchor", result["identity_evidence"])

    def test_competing_ids_in_either_type_block_even_when_alignment_truncates(self):
        wrong_type = node("wrong_type", self.kg["name"], "An invalid source identifier.",
                          kind="Gene", standard_ids=self.kg["standard_ids"])
        same_type = node("same_type", self.kg["name"], "A second nominally identical concept.",
                         standard_ids=self.kg["standard_ids"])
        result = realign(self.kg, [self.inserted, wrong_type, same_type], self.inserted["id"],
                         output=self.output, max_candidates=1)
        self.assertFalse(result["accepted"])
        self.assertCountEqual(result["identity_evidence"]["unresolved_exact_matches"], ["wrong_type", "same_type"])

    def test_affirmative_alignment_conflict_vetoes_direct_source_anchor(self):
        result = realign(self.kg, [self.inserted], self.inserted["id"], output=self.output,
                         model=AlignmentFixture({self.inserted["id"]: "UNMATCHED"}))
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "alignment_assessment_conflicts_with_expected_identity")


if __name__ == "__main__":
    unittest.main()
