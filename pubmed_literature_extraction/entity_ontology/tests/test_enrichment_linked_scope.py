"""Keep specific authoritative parents when separately sourced descriptions differ."""

from copy import deepcopy
from unittest import TestCase

from entity_ontology.enrichment.placement import place_verified
from entity_ontology.hierarchy.entity_placement_dag import EntityDAG
from entity_ontology.hierarchy.ontology_ols import SUBCLASS_OF
from entity_ontology.tests.test_enrichment_parent_fallback import lookup_for, node, term
from entity_ontology.tests.test_enrichment_parent_selection import fallback_request


def authority(source, ident, name, definition, **fields):
    return {**term(ident, name, definition=definition), "source": source, **fields}


def assertions(child, *parents):
    return [{"child_id": child["term_id"], "parent_id": parent["term_id"],
             "source": child["source"], "parent_source": parent["source"], "predicate": "is_a",
             "field": SUBCLASS_OF, "source_url": child["source_url"]} for parent in parents]


def gene_case():
    gene_id, hgnc_id = "NCBIGene:987654321", "HGNC:87654321"
    child = authority("HGNC", hgnc_id, "DEMO1", "Approved nomenclature: Demo locus with a protein product.",
                      match="verified_database_cross_reference", identity_evidence={
                          "cross_reference": gene_id, "record_id": hgnc_id, "database": "HGNC",
                          "canonical_label": "DEMO1", "taxon_id": "9606",
                          "field_values": {"locus_type": "gene with protein product"},
                          "url": "https://reference.example/nomenclature",
                      })
    parent = authority("SO", "SO:0009989", "Protein-encoding locus class",
                       "A gene that encodes a protein.")
    child["parent_relations"] = assertions(child, parent)
    concept = {**node("gene", "DEMO1", kind="Gene", references=[child]),
               "definition": "Human gene DEMO1. Independent Gene database description.",
               "taxon_id": "NCBITaxon:9606", "standard_ids": [gene_id]}
    return concept, parent


def protein_case():
    child = authority("PRO", "PR:Q0X991", "Human demo protein", "A translated product of the human DEMO2 locus.",
                      taxon_ids=["9606"], database_xrefs=["UniProtKB:Q0X991"],
                      match="verified_database_cross_reference", identity_evidence={
                          "cross_reference": "UniProt:Q0X991",
                          "kg_record": {"path": "/kg.json", "json_pointer": "/nodes/17"},
                          "grounding": [{"standard_ids": ["UniProt:Q0X991"],
                                         "artifact_record": {"path": "/grounded.json", "json_pointer": "/17"}}],
                      })
    family = authority("PRO", "PR:000999991", "Demo ortholog family",
                       "A protein translated from the DEMO2 locus or from one of its one-to-one orthologs.")
    species = authority("PRO", "PR:000999992", "Model species protein",
                        "A protein that is encoded in the genome of some Example species.",
                        taxon_ids=["9606"], synonyms=["Example species protein"])
    child["parent_relations"] = assertions(child, family, species)
    concept = {**node("protein", "Human demo protein", kind="Protein", references=[child]),
               "definition": "Curated snapshot annotation describing the protein's cellular role.",
               "taxon_id": "9606", "standard_ids": ["STRING:9606.ENSP123"],
               "enrichment_provenance": {"kg_record": deepcopy(child["identity_evidence"]["kg_record"])}}
    return concept, family, species


def uninformative(_, rows):
    return [{**row, "informative": False, "reason": "The model considers this class too broad."} for row in rows]


class LinkedScopeTest(TestCase):
    def test_exact_locus_cross_reference_keeps_direct_protein_encoding_class(self):
        concept, parent = gene_case()
        dag = EntityDAG(["Gene"])
        stage, audit = place_verified(dag, concept, parent_lookup=lookup_for(parent),
                                      request=fallback_request(decide=uninformative))
        imported, = audit["generated_parent_ids"]
        self.assertEqual(stage.nodes[imported]["standard_ids"], [parent["term_id"]])
        self.assertIn((concept["id"], imported), stage.edges)
        row, = audit["parent_fallback"]["candidates"]
        self.assertEqual(row["identity_scope_basis"], {"child": "hgnc_ncbi_gene_cross_reference",
                                                       "parent": "imported_authority_record"})
        self.assertEqual(row["review"]["relation_type"], "IS_A")
        self.assertTrue(row["review"]["informative"])
        self.assertFalse(row["review"]["model_review"]["informative"])
        self.assertEqual(audit["edge_evidence"][0]["reference"]["is_a_path"], row["path"])

    def test_exact_protein_record_keeps_ortholog_class_and_filters_species_only(self):
        concept, family, species = protein_case()
        stage, audit = place_verified(EntityDAG(["Protein"]), concept, parent_lookup=lookup_for(family, species),
                                      request=fallback_request(decide=uninformative))
        imported, = audit["generated_parent_ids"]
        self.assertEqual(stage.nodes[imported]["standard_ids"], [family["term_id"]])
        specific, generic = audit["parent_fallback"]["candidates"]
        self.assertEqual(specific["identity_scope_basis"]["child"], "pro_uniprot_cross_reference")
        self.assertTrue(specific["review"]["informative"])
        self.assertTrue(generic["deterministic_scope"])
        self.assertEqual(generic["review"]["relation_type"], "IS_A")
        self.assertEqual(generic["preference"]["status"], "not_informative")

    def test_unlinked_cross_reference_does_not_seed_authoritative_parent_search(self):
        for change in ("different_record", "different_taxon", "competing_accessions", "different_xref"):
            with self.subTest(change=change):
                concept, family, species = protein_case()
                record, = concept["reference_terms"]
                if change == "different_record":
                    concept["enrichment_provenance"]["kg_record"]["json_pointer"] = "/nodes/18"
                elif change == "different_taxon":
                    concept["taxon_id"] = "10090"
                elif change == "competing_accessions":
                    concept["standard_ids"].append("UniProt:Q0X992")
                else:
                    record["database_xrefs"] = ["UniProtKB:Q0X992"]
                lookup = lookup_for(family, species)
                dag = EntityDAG(["Protein"])
                stage, audit = place_verified(dag, concept, parent_lookup=lookup,
                                              request=fallback_request(decide=uninformative))
                self.assertIs(stage, dag)
                self.assertEqual(audit["parent_fallback"]["status"], "no_verified_child_identity")
                lookup.assert_not_called()

    def test_alias_only_local_parent_still_needs_informative_scope_review(self):
        concept, parent = gene_case()
        dag = EntityDAG(["Gene"])
        dag.place(node("alias", parent["name"], kind="Gene"))
        stage, audit = place_verified(dag, concept, parent_lookup=lookup_for(parent),
                                      request=fallback_request(decide=uninformative))
        self.assertIs(stage, dag)
        row, = audit["parent_fallback"]["candidates"]
        self.assertIsNone(row["identity_scope_basis"]["parent"])
        self.assertEqual(row["preference"]["status"], "not_informative")

    def test_local_exact_id_with_competing_same_authority_id_blocks_match(self):
        concept, parent = gene_case()
        dag = EntityDAG(["Gene"])
        dag.place({**node("local", parent["name"], kind="Gene"), "definition": parent["definition"],
                   "standard_ids": [parent["term_id"], "SO:0009988"]})
        stage, audit = place_verified(dag, concept, parent_lookup=lookup_for(parent),
                                      request=fallback_request(decide=uninformative))
        self.assertIs(stage, dag)
        row, = audit["parent_fallback"]["candidates"]
        self.assertEqual(row["status"], "local_identity_ambiguous_or_conflicting_type")

    def test_verified_local_authority_record_can_be_reused_with_updated_wording(self):
        concept, parent = gene_case()
        dag = EntityDAG(["Gene"])
        recorded = {**parent, "definition": "A gene that codes for a translated polypeptide."}
        dag.place({**node("verified", parent["name"], kind="Gene"), "definition": recorded["definition"],
                   "standard_ids": [parent["term_id"]], "definition_evidence": recorded})
        stage, audit = place_verified(dag, concept, parent_lookup=lookup_for(parent),
                                      request=fallback_request(decide=uninformative))
        self.assertIn((concept["id"], "verified"), stage.edges)
        self.assertEqual(audit["generated_parent_ids"], [])
        row, = audit["parent_fallback"]["candidates"]
        self.assertEqual(row["identity_scope_basis"]["parent"], "bound_authority_record")
