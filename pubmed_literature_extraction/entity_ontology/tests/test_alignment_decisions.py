"""Admission regressions: retrieval similarity must never become an edge alone."""

import unittest

from entity_ontology.alignment.decisions import admit, choose, validate_assessments
from entity_ontology.alignment.loading import normalize_kg_node
from entity_ontology.hierarchy.ranking_journal import ContractError


def node(ident, **changes):
    result = {
        "id": ident, "name": "liver tissue", "kind": "Tissue",
        "definition": "Liver tissue is the tissue forming the liver organ.",
        "aliases": [], "standard_ids": [], "parent_ids": [], "is_root": False,
        "entity_level": "class", "entity_level_evidence": [],
    }
    return {**result, **changes}


def assessment(kg, ontology, relation="MAPS_TO", **changes):
    result = {
        "id": ontology["id"], "relation_type": relation, "confidence": "high",
        "kg_entity_level": kg.get("entity_level", "unknown"),
        "ontology_entity_level": ontology.get("entity_level", "unknown"),
        "same_scope": True, "rationale": "Assess the supplied definitions and entity levels.",
        "evidence": [{"side": side, "field": "definition", "quote": row["definition"]}
                     for side, row in (("kg", kg), ("ontology", ontology)) if row["definition"]],
    }
    return {**result, **changes}


class AlignmentAdmissionTests(unittest.TestCase):
    def test_exact_alias_alone_cannot_establish_identity(self):
        kg, ontology = node("kg", definition=""), node("ontology", definition="")
        proposal = assessment(kg, ontology, evidence=[
            {"side": "kg", "field": "name", "quote": "liver tissue"},
            {"side": "ontology", "field": "name", "quote": "liver tissue"},
        ])
        result = admit(proposal, kg, ontology)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "identity_evidence_missing")

    def test_bare_shared_numbers_are_not_standard_identity(self):
        kg = node("kg", definition="", standard_ids=["123"])
        ontology = node("ontology", definition="", standard_ids=["123"])
        result = admit(assessment(kg, ontology), kg, ontology)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "identity_evidence_missing")

    def test_namespace_variants_are_canonical_identity_evidence(self):
        kg = node("kg", kind="Gene", definition="", standard_ids=["NCBIGene:123"])
        ontology = node("ontology", kind="Gene", definition="", standard_ids=["entrez:123"])
        result = admit(assessment(kg, ontology), kg, ontology)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "shared_standard_id")

    def test_hgnc_lookup_does_not_invent_missing_species(self):
        kg = node("kg", kind="Gene", definition="", taxon_id="9606")
        ontology = node("ontology", kind="Gene", definition="")
        reference = {"source": "HGNC", "sources_checked": ["HGNC"], "relations": [], "terms": [
            {"source": "HGNC", "concept_id": ident, "term_id": "HGNC:123"}
            for ident in (kg["id"], ontology["id"])
        ]}
        result = admit(assessment(kg, ontology), kg, ontology, reference)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "species_scope_not_established_on_both_sides")

    def test_known_species_cannot_map_to_unspecified_scope_using_function_only(self):
        kg = node("kg", kind="Protein", taxon_id="9606")
        ontology = node("ontology", kind="Protein")
        result = admit(assessment(kg, ontology), kg, ontology)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "species_scope_not_established_on_both_sides")

    def test_class_cannot_be_promoted_to_instance(self):
        kg, ontology = node("kg"), node("ontology")
        result = admit(assessment(kg, ontology, "INSTANCE_OF"), kg, ontology)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "class_to_class_requires_review")
        self.assertEqual(result["class_inclusion_proposal"], {"child_id": "kg", "parent_id": "ontology"})

    def test_class_inclusion_is_recorded_without_mapping(self):
        kg, ontology = node("kg"), node("ontology")
        result = admit(assessment(kg, ontology, "CLASS_SUBSUMPTION"), kg, ontology)
        self.assertFalse(result["accepted"])
        self.assertIn("class_inclusion_proposal", result)
        selected, status, _ = choose([result], {"ontology": ontology}, {})
        self.assertIsNone(selected)
        self.assertEqual(status, "REVIEW_REQUIRED")

    def test_explicit_species_conflict_overrides_shared_identifier(self):
        kg = node("kg", taxon_id="NCBITaxon:9606", standard_ids=["UBERON:0002107"])
        ontology = node("ontology", taxon_id="10090", standard_ids=["UBERON:0002107"])
        result = admit(assessment(kg, ontology), kg, ontology)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "species_conflict")

    def test_imported_ncbi_taxon_reaches_admission_guard(self):
        kg = normalize_kg_node({"element_id": "kg", "labels": ["Protein"], "properties": {
            "name": "enzyme", "annotation": "An enzyme involved in a specific metabolic reaction.",
            "ncbi_taxon_id": 9606, "species_name": "Homo sapiens",
        }})
        ontology = node("ontology", kind="Protein", taxon_id="10090")
        result = admit(assessment(kg, ontology), kg, ontology)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "species_conflict")

    def test_scope_mismatch_blocks_same_name(self):
        kg, ontology = node("kg"), node("ontology")
        result = admit(assessment(kg, ontology, same_scope=False), kg, ontology)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "scope_mismatch")

    def test_fabricated_quote_cannot_support_shared_id(self):
        kg = node("kg", standard_ids=["UBERON:0002107"])
        ontology = node("ontology", standard_ids=["UBERON:0002107"])
        proposal = assessment(kg, ontology)
        proposal["evidence"][0]["quote"] = "This exact sentence does not occur in the source."
        result = admit(proposal, kg, ontology)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "unverifiable_source_citation")

    def test_gene_and_protein_are_not_same_entity(self):
        kg, ontology = node("kg", kind="Gene"), node("ontology", kind="Protein")
        result = admit(assessment(kg, ontology), kg, ontology)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "type_mismatch_or_root")

    def test_explicit_individual_with_membership_can_align(self):
        kg = node("sample", name="sample liver-01", entity_level="individual",
                  entity_level_evidence=[{"property": "entity_level", "value": "individual"}],
                  definition="Sample liver-01 is an individual specimen of human liver tissue.")
        ontology = node("liver-class")
        result = admit(assessment(kg, ontology, "INSTANCE_OF"), kg, ontology)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "explicit_individual_and_membership_evidence")

    def test_model_cannot_invent_individual_status(self):
        kg, ontology = node("kg", entity_level="unknown"), node("ontology")
        proposal = assessment(kg, ontology, "INSTANCE_OF", kg_entity_level="individual")
        result = admit(proposal, kg, ontology)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "individual_level_not_established")

    def test_model_cannot_recast_explicit_individual_target_as_class(self):
        kg = node("kg", entity_level="individual", entity_level_evidence=["Explicit specimen record"])
        ontology = node("ontology", entity_level="individual")
        proposal = assessment(kg, ontology, "INSTANCE_OF", ontology_entity_level="class")
        self.assertFalse(admit(proposal, kg, ontology)["accepted"])

    def test_maps_to_respects_explicit_levels_even_when_model_overrides(self):
        kg, ontology = node("kg", entity_level="individual"), node("ontology", entity_level="class")
        proposal = assessment(kg, ontology, kg_entity_level="class")
        self.assertFalse(admit(proposal, kg, ontology)["accepted"])

    def test_multiple_equivalent_candidates_require_review(self):
        kg, first, second = node("kg"), node("first"), node("second")
        admitted = [admit(assessment(kg, n), kg, n) for n in (first, second)]
        selected, status, reason = choose(admitted, {n["id"]: n for n in (first, second)}, {})
        self.assertIsNone(selected)
        self.assertEqual((status, reason), ("REVIEW_REQUIRED", "competing_identity_candidates"))

    def test_most_specific_supported_class_need_not_be_leaf(self):
        kg = node("kg", entity_level="individual", entity_level_evidence=["Explicit specimen record"])
        broad = node("broad")
        specific = node("specific", parent_ids=["broad"])
        leaf = node("leaf", parent_ids=["specific"])
        admitted = [admit(assessment(kg, n, "INSTANCE_OF"), kg, n) for n in (broad, specific)]
        selected, status, _ = choose(admitted, {n["id"]: n for n in (broad, specific, leaf)}, {})
        self.assertEqual(status, "ALIGNED")
        self.assertEqual(selected["ontology_node_id"], "specific")

    def test_unresolved_more_specific_candidate_prevents_forced_match(self):
        kg = node("kg", entity_level="individual", entity_level_evidence=["Explicit specimen record"])
        broad, specific = node("broad"), node("specific", parent_ids=["broad"])
        admitted = [admit(assessment(kg, broad, "INSTANCE_OF"), kg, broad),
                    admit(assessment(kg, specific, "UNCERTAIN", confidence="low"), kg, specific)]
        selected, status, _ = choose(admitted, {n["id"]: n for n in (broad, specific)}, {})
        self.assertIsNone(selected)
        self.assertEqual(status, "REVIEW_REQUIRED")

    def test_truncation_does_not_hide_competing_matches(self):
        kg, ontology = node("kg"), node("ontology")
        admitted = [admit(assessment(kg, ontology), kg, ontology)]
        selected, status, reason = choose(admitted, {ontology["id"]: ontology},
                                          {"truncated": True, "exact_candidate_count": 2,
                                           "returned_candidates": 1})
        self.assertIsNone(selected)
        self.assertEqual((status, reason), ("REVIEW_REQUIRED", "candidate_limit_truncated"))

    def test_response_cannot_skip_or_duplicate_candidates(self):
        kg, ontology = node("kg"), node("ontology")
        proposal = assessment(kg, ontology)
        for rows in ([], [proposal, proposal]):
            with self.subTest(rows=len(rows)), self.assertRaises(ContractError):
                validate_assessments({"rows": rows}, [ontology["id"]])


if __name__ == "__main__":
    unittest.main()
