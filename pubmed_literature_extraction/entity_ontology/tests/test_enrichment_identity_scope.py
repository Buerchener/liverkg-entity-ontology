"""Scoped identity proofs preserve subtype distinctions without trusting aliases."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from entity_ontology.enrichment.verification import realign, verify_concept
from entity_ontology.tests.test_enrichment_verification import AlignmentFixture, node


class ScopeFixture(AlignmentFixture):
    def assess(self, payload):
        rows = super().assess(payload)
        for row in rows:
            if row["relation_type"] == "CLASS_SUBSUMPTION":
                row.update(confidence="medium", same_scope=True)
        return rows


def assertion(child, parent, predicate="is_a"):
    return {
        "child_id": child, "parent_id": parent, "source": "PRO", "parent_source": "PRO",
        "predicate": predicate, "field": "http://www.w3.org/2000/01/rdf-schema#subClassOf",
        "source_url": f"https://reference.example/{child}",
    }


class IdentityScopeTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.output = Path(work.name)
        human, family = "PR:P00001", "PR:000000012"
        self.link = assertion(human, family)
        kg_definition = "A specific human translation product linked to its curated sequence record."
        family_definition = "Translation products of the gene in human and its one-to-one orthologs."
        self.kg = {
            **node("4:fixture:7", "Symbol", kg_definition, kind="Protein",
                   standard_ids=["STRING:9606.ENSP123"]),
            "source": {"database": "fixture", "element_id": "4:fixture:7"},
            "species": "Homo sapiens", "taxon_id": "9606",
            "definition_evidence": {"database": "fixture", "definition": kg_definition},
            "prior_grounding": [{"standard_ids": ["UniProt:P00001"],
                                 "artifact_record": {"path": "/fixture.json", "json_pointer": "/3"}}],
            "reference_terms": [{
                "source": "PRO", "term_id": human, "name": "Human product",
                "definition": "The product of the gene in the human genome.",
                "source_url": f"https://reference.example/{human}",
                "identity_evidence": {"cross_reference": "UniProt:P00001"},
                "database_xrefs": ["UniProtKB:P00001"], "taxon_ids": ["9606"],
                "parent_relations": [self.link],
            }],
        }
        self.family = {
            **node("family", "Protein across orthologs", family_definition,
                   kind="Protein", standard_ids=[family]),
            "aliases": ["Symbol"],
            "origin": "kg_enrichment_reference_parent",
            "definition_evidence": {
                "source": "PRO", "term_id": family, "name": "Protein across orthologs",
                "definition": family_definition, "source_url": f"https://reference.example/{family}",
            },
        }

    def _copy(self, **changes):
        return {
            **deepcopy(self.kg), "id": "copy", "parent_ids": [self.family["id"]],
            "enrichment_provenance": {
                "kg_source": self.kg["source"], "definition_evidence": self.kg["definition_evidence"],
                "reference_terms": self.kg["reference_terms"],
            },
            **changes,
        }

    def _realign(self, parent, *, relation="CLASS_SUBSUMPTION", **kwargs):
        return realign(self.kg, [self._copy(), parent], "copy", output=self.output, require_source_anchor=True,
                       model=ScopeFixture({"copy": "MAPS_TO", parent["id"]: relation}), **kwargs)

    def test_verified_scope_can_disambiguate_existing_same_name_broader_class_before_insertion(self):
        result = verify_concept(self.kg, [self.family], output=self.output,
                                model=ScopeFixture({"family": "CLASS_SUBSUMPTION"}))
        self.assertEqual(result["action"], "new")
        typed, = result["identity_evidence"]["scope_resolutions"]
        self.assertEqual(typed["relation"], "IS_A")
        self.assertEqual(typed["authority"]["path"], [self.link])

    def test_verified_scope_keeps_normal_alignment_only_on_exact_copy(self):
        result = self._realign(self.family)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["alignment"]["ontology_node_id"], "copy")
        self.assertEqual(result["alignment"]["decision_status"], "ALIGNED")
        scopes = {row["ontology_node_id"]: row["relation"] for row in result["identity_evidence"]["scope_resolutions"]}
        self.assertEqual(scopes, {"copy": "EQUIVALENT", "family": "IS_A"})

    def test_exact_but_unrelated_alias_is_not_silently_resolved(self):
        self.kg["reference_terms"][0]["parent_relations"] = [dict(self.link, predicate="part_of")]
        self.kg["auxiliary_evidence"] = [{
            "subject_id": self.link["child_id"], "object_id": self.link["parent_id"],
            "source_url": self.link["source_url"], "relation": "RELATED",
        }]
        result = self._realign(self.family, relation="UNCERTAIN")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["identity_evidence"]["unresolved_exact_matches"], ["family"])
        proof = next(row for row in result["identity_evidence"]["scope_resolutions"]
                     if row["ontology_node_id"] == "family")
        self.assertEqual(proof["relation"], "RELATED")

    def test_two_stage_proof_needs_bound_endpoints_and_every_hop_must_be_is_a(self):
        middle = "PR:000000034"
        first = assertion(self.link["child_id"], middle)
        self.kg["reference_terms"][0]["parent_relations"] = [first]
        evidence = {
            "child_id": "copy", "parent_id": "family",
            "typed_relation_review": {"relation_type": "IS_A", "same_endpoint_scope": True},
            "reference": {
                "start": self.kg["reference_terms"][0], "parent_term": self.family["definition_evidence"],
                "is_a_path": [first, assertion(middle, self.link["parent_id"])],
            },
        }
        self.assertTrue(self._realign(self.family, placement_evidence=[evidence])["accepted"])
        evidence["reference"]["is_a_path"][1]["predicate"] = "part_of"
        self.assertFalse(self._realign(self.family, placement_evidence=[evidence])["accepted"])

    def test_sharing_a_stable_id_keeps_collision_even_with_proven_scope_difference(self):
        family = {**self.family, "standard_ids": [*self.family["standard_ids"], *self.kg["standard_ids"]]}
        result = self._realign(family)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["identity_evidence"]["unresolved_exact_matches"], ["family"])
        self.assertEqual(result["identity_evidence"]["scope_resolutions"][1]["relation"], "CONFLICT")

    def test_conflicting_species_blocks_ordinary_alignment_with_shared_id(self):
        copy = self._copy(species="Mus musculus")
        result = verify_concept(self.kg, [copy], output=self.output,
                                model=ScopeFixture({"copy": "MAPS_TO"}))
        self.assertEqual(result["alignment"]["decision_status"], "ALIGNED")
        self.assertEqual(result["action"], "needs_review")
        self.assertEqual(result["identity_evidence"]["scope_resolutions"][0]["basis"], "explicit_species_conflict")

    def test_conflicting_taxon_fields_do_not_admit_snapshot_copy(self):
        self.kg["ncbi_taxon_id"] = "10090"
        result = realign(self.kg, [self._copy()], "copy", output=self.output,
                         model=ScopeFixture({"copy": "MAPS_TO"}), require_source_anchor=True)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["identity_evidence"]["scope_resolutions"][0]["basis"], "explicit_taxon_conflict")

    def test_competing_authority_ids_remain_ambiguous_on_same_snapshot_copy(self):
        self.kg["standard_ids"].append("STRING:9606.ENSP456")
        result = realign(self.kg, [self._copy()], "copy", output=self.output,
                         model=ScopeFixture({"copy": "MAPS_TO"}), require_source_anchor=True)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["identity_evidence"]["scope_resolutions"][0]["basis"], "ambiguous_authority_identifiers")

    def test_duplicate_authority_identity_is_not_hidden_by_bounded_name_retrieval(self):
        shared = self.kg["reference_terms"][0]
        other = {
            **node("duplicate", shared["name"], shared["definition"],
                   kind="Protein", standard_ids=[shared["term_id"]]),
            "definition_evidence": shared,
        }
        result = realign(self.kg, [self._copy(), other], "copy", output=self.output,
                         model=ScopeFixture({"copy": "MAPS_TO"}), max_candidates=1)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["identity_evidence"]["unresolved_exact_matches"], ["duplicate"])


if __name__ == "__main__":
    unittest.main()
