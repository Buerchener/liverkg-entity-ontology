import json
import tempfile
import unittest
from pathlib import Path

from entity_ontology.enrichment.auxiliary import auxiliary_context


class AuxiliaryContextTests(unittest.TestCase):
    def test_pathway_source_keeps_annotation_and_event_containment_separate(self):
        node = {"kind": "Pathway", "standard_ids": ["Reactome:R-HSA-123"]}
        description = {"database": "Reactome", "record_id": node["standard_ids"][0]}
        root = {"schemaClass": "Pathway", "stId": "R-HSA-123", "goBiologicalProcess":
                {"accession": "0000007", "databaseName": "GO", "name": "example process"}}
        paths = [[root, {"schemaClass": "Pathway", "stId": "R-HSA-124", "displayName": "container"}]]
        def response(url):
            return json.dumps(paths if url.endswith("/ancestors") else root)
        with tempfile.TemporaryDirectory() as tmp:
            facts, observations = auxiliary_context(node, description, Path(tmp), response)
            self.assertEqual([f["relation"] for f in facts], ["RELATED", "PART_OF"])
            self.assertEqual([f["subject_id"] for f in facts], node["standard_ids"] * 2)
            self.assertEqual([f["object_id"] for f in facts], ["GO:0000007", "Reactome:R-HSA-124"])
            self.assertEqual(len(observations), 2)
            again, observations = auxiliary_context(node, description, Path(tmp), None)
            self.assertEqual(facts, again)
            self.assertTrue(all(o["cache_hit"] for o in observations))

    def test_uniprot_complex_membership_and_interpro_are_context_only(self):
        node = {"kind": "Protein", "taxon_id": "9606", "standard_ids": ["UniProt:Q9BZD4"]}
        record = {"primaryAccession": "Q9BZD4", "entryType": "UniProtKB reviewed (Swiss-Prot)",
                  "organism": {"taxonId": 9606}, "comments": [{"commentType": "SUBUNIT",
                  "texts": [{"value": "Component of the NDC80 complex."}]}],
                  "uniProtKBCrossReferences": [{"database": "InterPro", "id": "IPR005549"}]}
        with tempfile.TemporaryDirectory() as tmp:
            facts, _ = auxiliary_context(node, {}, Path(tmp), lambda _: json.dumps(record))
            self.assertEqual([f["relation"] for f in facts], ["RELATED", "RELATED"])
            self.assertEqual(facts[0]["predicate"], "SUBUNIT")
            facts, _ = auxiliary_context({**node, "taxon_id": "10090"}, {}, Path(tmp), None)
            self.assertEqual(facts, [])

    def test_mesh_broader_heading_is_never_a_strict_parent(self):
        node = {"kind": "Metabolite", "name": "compound", "standard_ids": []}
        with tempfile.TemporaryDirectory() as tmp:
            def fetch(url):
                if "/lookup/" in url:
                    return json.dumps([{"label": "compound", "resource": "http://id.nlm.nih.gov/mesh/D123456"}])
                return json.dumps({"identifier": "D123456", "label": {"@value": "compound"},
                                   "http://id.nlm.nih.gov/mesh/vocab#active": True,
                                   "broaderDescriptor": "http://id.nlm.nih.gov/mesh/D123450"})
            facts, _ = auxiliary_context(node, {}, Path(tmp), fetch)
            self.assertEqual(facts[0]["predicate"], "broaderDescriptor")
            self.assertEqual(facts[0]["relation"], "RELATED")
            grounded = {**node, "reference_terms": [{"parent_relations": [{"predicate": "is_a"}]}]}
            self.assertEqual(auxiliary_context(grounded, {}, Path(tmp), None), ([], []))
