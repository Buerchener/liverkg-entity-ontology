"""Verify bound authority definitions and quarantine records with incompatible scope."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from entity_ontology.enrichment.definition_authorities import hmdb_definition, hmdb_via_chebi, ncit_definition
from entity_ontology.enrichment.obo import OBOCatalog
from entity_ontology.enrichment.sources import DefinitionSources, _article_definition


class DefinitionEnrichmentTests(unittest.TestCase):
    def chemicals(self):
        kg = {"kind": "Metabolite", "name": "Example compound", "standard_ids": ["HMDB:HMDB0000001"],
              "source": {"properties": {"chemical_formula": "C2H6O"}}}
        record = {"id": 5, "chebi_accession": "CHEBI:5", "is_released": True, "definition": "Official description.",
                  "name": "Example compound", "chemical_data": {"formula": "C2H6O"},
                  "database_accessions": {"MANUAL_X_REF": [{"source_name": "HMDB", "accession_number": "HMDB0000001"}]},
                  "ontology_relations": {"outgoing_relations": [
                      {"init_id": 5, "final_id": 2, "relation_type": "has role"},
                      {"init_id": 5, "final_id": 3, "relation_type": "is a"}]}}
        result = {"total": 1, "results": [{"_source": {"chebi_accession": "CHEBI:5"}}]}
        def fetch(url):
            return json.dumps(result if "/es_search/" in url else record)
        return kg, record, fetch

    def test_hmdb_accession_bound_to_chebi_and_only_forward_subclass_edges_retained(self):
        node, _, read = self.chemicals()
        result = hmdb_via_chebi(node, read, [])
        self.assertEqual(result["verified_standard_ids"], ["CHEBI:5"])
        self.assertEqual(result["direct_parent_ids"], ["CHEBI:3"])
        self.assertEqual(result["cross_reference"]["entry"]["accession_number"], "HMDB0000001")
        self.assertEqual(result["parent_relations"][0]["child_id"], "CHEBI:5")

    def test_hmdb_wrong_formula_xref_or_canonical_identity_cannot_bind(self):
        for mismatch in ("formula", "xref", "authority_id"):
            node, record, read = self.chemicals()
            if mismatch == "formula":
                record["chemical_data"]["formula"] = "C3H8O"
            elif mismatch == "xref":
                record["database_accessions"]["MANUAL_X_REF"][0]["accession_number"] = "HMDB0000009"
            else:
                node["standard_ids"].append("CHEBI:99")
            self.assertIsNone(hmdb_via_chebi(node, read, []), mismatch)

    def test_native_hmdb_supplied_id_and_name_required(self):
        node, _, _ = self.chemicals()
        record = """<metabolite xmlns="http://www.hmdb.ca"><accession>HMDB0000001</accession>
                    <name>Example compound</name><description>Original record text.</description></metabolite>"""
        self.assertEqual(hmdb_definition(node, "HMDB0000001", lambda url: record, [])["definition"],
                         "Original record text.")
        self.assertIsNone(hmdb_definition(node, "HMDB0000009", lambda url: record, []))

    def test_cited_preferred_name_requires_active_whole_search_and_compatible_type(self):
        node = {"kind": "Tissue", "name": "Example sample", "standard_ids": []}
        record = {"active": True, "terminology": "ncit", "code": "C1234", "name": node["name"],
                  "properties": [{"code": "P106", "value": "Body Substance"}],
                  "definitions": [{"source": "NCI", "definition": "Original source text."}]}
        search = {"total": 1, "concepts": [record]}
        read = lambda url: json.dumps(search if "/search?" in url else record)
        evidence = ncit_definition(node, read, [])
        self.assertTrue(evidence["scope_review_required"])
        self.assertNotIn("verified_standard_ids", evidence)
        record["properties"][0]["value"] = "Tissue"
        self.assertEqual(ncit_definition(node, read, [])["verified_standard_ids"], ["NCIT:C1234"])
        search["total"] = 12
        self.assertIsNone(ncit_definition(node, read, []))

    def test_cited_paper_must_define_the_term_explicitly(self):
        node = {"name": "signal condition"}
        context = {"pmid": "123456", "url": "https://pubmed.ncbi.nlm.nih.gov/123456/", "artifact": "original.jsonl",
                   "mention_excerpts": ["Signal condition occurs in the affected animals."]}
        self.assertIsNone(_article_definition(node, context))
        context["mention_excerpts"].append("Signal condition is defined as a reproducible duration change.")
        evidence = _article_definition(node, context)
        self.assertEqual(evidence["definition"], context["mention_excerpts"][-1])
        context["mention_excerpts"].append("Signal condition denotes a different observation.")
        self.assertIsNone(_article_definition(node, context))

    def test_prior_id_needs_corroborating_label_and_preserves_pointer(self):
        with TemporaryDirectory() as folder:
            obo = Path(folder) / "cl.obo"
            obo.write_text('data-version: fixture\n[Term]\nid: CL:12345\nname: example erythrocyte\n'
                           'def: "Official cell definition." []\n')
            evidence_pointer = {"path": "screening.json", "json_pointer": "/screened_rows/8"}
            node = {"kind": "CellType", "name": "example erythrocytes", "standard_ids": [], "aliases": [],
                    "prior_grounding": [{"standard_ids": ["CL:12345"], "artifact_record": evidence_pointer}]}
            src = DefinitionSources(Path(folder) / "cache", obo=OBOCatalog({"CellType": obo}), allow_ols=False)
            out, _ = src.resolve(node)
            self.assertEqual(out["standard_ids"], ["CL:12345"])
            self.assertEqual(out["definition_evidence"]["prior_grounding"][0]["artifact_record"], evidence_pointer)
            node["name"] = "unrelated cell"
            self.assertIsNone(src.resolve(node)[1]["evidence"])

    def test_generic_literature_type_stays_unbound(self):
        with TemporaryDirectory() as folder:
            catalog = Mock()
            src = DefinitionSources(Path(folder), catalog=catalog)
            _, audit = src.resolve({"name": "metabolites", "kind": "Metabolite", "aliases": [], "standard_ids": []})
            self.assertIsNone(audit["evidence"])
            self.assertEqual(audit["observations"][-1]["status"], "generic_type_without_specific_identity")
            catalog.verify_endpoint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
