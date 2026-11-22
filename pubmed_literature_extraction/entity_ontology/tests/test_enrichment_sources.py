"""Check that locally recovered definitions keep their authoritative scope."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from entity_ontology.enrichment.sources import DefinitionSources, _abbreviation, _gene
from entity_ontology.enrichment.obo import OBOCatalog
from entity_ontology.hierarchy.ols_reference import OLSReferenceAgent
from entity_ontology.hierarchy.ontology_ols import OntologyOLS, SUBCLASS_OF
from entity_ontology.hierarchy.placement import describe, review_item
from entity_ontology.hierarchy.ols_client import OBO
from entity_ontology.tests.test_ontology_ols import FakeOLS, hgnc_gene


class _Catalog:
    def verify_endpoint(self, source, node):
        if node["name"] != "test tissue":
            return {"terms": []}
        return {"terms": [{
            "term_id": "UBERON:0000001", "name": "test tissue",
            "definition": "A definition recorded by the fixture authority.",
            "source_url": "https://example.test/UBERON_0000001",
            "direct_parent_ids": [], "match": "exact_label",
        }]}


class DefinitionSourceTests(unittest.TestCase):
    def test_unambiguous_literature_alias_reaches_authority_lookup(self):
        with TemporaryDirectory() as tmp:
            articles = Path(tmp) / "articles.jsonl"
            articles.write_text(json.dumps({
                "pmid": "11111111", "title": "Results from test tissue (TT).", "abstract": "",
            }), encoding="utf-8")
            sources = DefinitionSources(Path(tmp) / "cache", articles=[articles], catalog=_Catalog())
            node = {"name": "TT", "kind": "Tissue", "aliases": [], "standard_ids": [],
                    "source_context": {"source": "PubMed:11111111"}}
            enriched, audit = sources.resolve(node)
        self.assertEqual(enriched["aliases"], ["test tissue"])
        self.assertEqual(enriched["name"], "TT")
        self.assertEqual(audit["evidence"]["record_id"], "UBERON:0000001")
        self.assertEqual(audit["original_context"]["mention_excerpts"], ["Results from test tissue (TT)."])
        self.assertIsNone(_abbreviation("first group (FG). Fast growth (FG).", "FG"))

    def test_gene_identity_stays_explicit_if_summary_describes_product(self):
        body = json.dumps({"result": {"123": {
            "uid": "123", "organism": {"taxid": 9606}, "name": "XYZ",
            "nomenclaturename": "fixture gene", "summary": "Predicted to produce a binding protein.",
        }}})
        evidence = _gene("https://example.test/123", body, "123", {"name": "XYZ"})
        self.assertTrue(evidence["definition"].startswith("Human gene XYZ. NCBI Gene name: fixture gene."))
        self.assertEqual(evidence["field_values"]["summary"], "Predicted to produce a binding protein.")

    def test_normalized_cell_names_keep_verified_obo_term_and_cited_direct_edge(self):
        cells = [("CL:0000636", "Mueller cell", "Müller glia", "CL:0000681"),
                 ("CL:0002062", "type I pneumocyte", "type I alveolar cell", "CL:0000322")]
        with TemporaryDirectory() as folder:
            obo = Path(folder) / "cl.obo"
            obo.write_text("data-version: test\n" + "".join(
                f'\n[Term]\nid: {ident}\nname: {official}\ndef: "Fixture cell." []\n'
                f'synonym: "{alias}" EXACT []\nis_a: {parent}\nrelationship: part_of CL:999\n'
                for ident, official, alias, parent in cells), encoding="utf-8")
            sources = DefinitionSources(folder, allow_ols=False, obo=OBOCatalog({"CellType": obo}))
            for (ident, official, _, parent), query in zip(cells, ("müller glia", "alveolar cells type 1")):
                with self.subTest(ident=ident):
                    node, _ = sources.resolve({"id": "kg", "name": query, "kind": "CellType",
                                               "aliases": [], "standard_ids": []})
                    endpoint = describe(node)
                    authority, = endpoint["reference_terms"]
                    self.assertEqual((authority["term_id"], authority["name"]), (ident, official))
                    edge, = authority["parent_relations"]
                    self.assertEqual((edge["child_id"], edge["parent_id"], edge["predicate"]),
                                     (ident, parent, "is_a"))
                    self.assertEqual(edge["artifact"]["release"], "test")
                    self.assertGreater(edge["line"], 0)
                    self.assertEqual(endpoint["definition_evidence"]["record_id"], ident)
                    stop = Mock(return_value=[{"id": "next", "action": "stop"}])
                    broader = {**endpoint, "id": "parent", "reference_terms": []}
                    investigation = OLSReferenceAgent(stop).lookup(endpoint, [review_item("q", endpoint, broader)])
                    self.assertEqual(investigation["terms"][0]["term_id"], ident)
                    self.assertEqual(investigation["tool_calls"], 0)

    def test_three_gene_loci_keep_hgnc_identity_and_only_direct_named_so_superclass(self):
        for symbol, entrez, hgnc_id, parent_id in (
            ("GABRD", "2563", "4084", "SO:0001217"), ("MIR30C2", "407032", "31627", "SO:0001265"),
            ("MIR221", "407006", "31601", "SO:0001265"),
        ):
            with self.subTest(symbol=symbol), TemporaryDirectory() as folder:
                parent_iri = OBO + parent_id.replace(":", "_")
                restriction = {"http://www.w3.org/2002/07/owl#someValuesFrom": OBO + "SO_9999999"}
                record = hgnc_gene(hgnc_id, symbol, "fixture locus", directParent=[parent_iri],
                                   hierarchicalParent=[parent_iri, OBO + "SO_9999999"],
                                   **{SUBCLASS_OF: [parent_iri, restriction]})
                ols = OntologyOLS(get_json=FakeOLS({}, record))
                hgnc = {"entrez_id": entrez, "symbol": symbol, "status": "Approved", "name": "fixture locus",
                        "hgnc_id": "HGNC:" + hgnc_id, "locus_type": "fixture gene class"}
                sources = DefinitionSources(folder, catalog=ols,
                                            read_url=lambda url: json.dumps({"response": {"docs": [hgnc]}}))
                node = {"id": "kg", "kind": "Gene", "name": symbol, "aliases": [],
                        "standard_ids": ["NCBIGene:" + entrez], "taxon_id": "9606",
                        "definition": "Sourced test definition.", "source": {"element_id": "test"}}
                result, _ = sources.resolve(node)
                endpoint = describe(result)
                self.assertEqual(endpoint["standard_ids"], ["NCBIGene:" + entrez])
                term, = endpoint["reference_terms"]
                edge, = term["parent_relations"]
                self.assertEqual((edge["child_id"], edge["parent_id"], edge["parent_source"]),
                                 ("HGNC:" + hgnc_id, parent_id, "SO"))
                self.assertEqual(term["identity_evidence"]["field_values"]["locus_type"], "fixture gene class")
                self.assertEqual(edge["field"], SUBCLASS_OF)
                self.assertEqual(term["definition_source"], term["identity_evidence"]["url"])


if __name__ == "__main__":
    unittest.main()
