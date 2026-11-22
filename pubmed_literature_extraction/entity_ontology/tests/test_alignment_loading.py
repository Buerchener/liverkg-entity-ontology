"""Tests for identity preservation and read-only alignment data adapters."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from entity_ontology.alignment.loading import (
    COUNTS_QUERY, RELATION_COUNT_QUERY, SCHEMA_QUERY, SAMPLE_QUERY,
    extract_standard_ids, load_kg_snapshot, load_ontology, normalize_kg_node, read_neo4j,
)


class AlignmentLoadingTests(unittest.TestCase):
    def _snapshot(self, node, *, metadata=None):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "kg.json"
            path.write_text(json.dumps({"nodes": [node], "metadata": {} if metadata is None else metadata}))
            before = path.read_bytes()
            result = load_kg_snapshot(path)
            self.assertEqual(path.read_bytes(), before)
            return result

    def test_valid_snapshot_preserves_all_node_evidence(self):
        node = normalize_kg_node({"element_id": "4:db:7", "labels": ["Gene"], "properties": {
            "name": "GENE1", "description": "An existing source definition.",
            "gene_id": "NCBIGene:123", "synonyms": ["Alias"], "entity_level": "class",
        }}, database="example")
        node["custom_evidence"] = {"keep": ["unchanged"]}
        result = self._snapshot(node, metadata={"sample_strategy": "existing"})
        self.assertEqual(result["nodes"], [node])
        self.assertEqual(result["metadata"]["sample_strategy"], "existing")
        self.assertEqual(len(result["metadata"]["snapshot_file"]["sha256"]), 64)

    def test_snapshot_normalizes_kind_and_defaults_missing_optional_fields(self):
        result = self._snapshot({"id": "k", "name": "A", "kind": "gene"})["nodes"][0]
        self.assertEqual(result["kind"], "Gene")
        self.assertEqual(result["definition"], "")
        self.assertEqual(result["entity_level"], "unknown")
        for field in ("aliases", "standard_ids", "parent_ids"):
            self.assertEqual(result[field], [])

    def test_snapshot_rejects_nonstrings_inside_retrieval_lists(self):
        for field in ("aliases", "standard_ids", "parent_ids"):
            for wrong in ([{}], [1], [None], "not a list", None):
                with self.subTest(field=field, wrong=wrong), self.assertRaisesRegex(ValueError, "list of strings"):
                    self._snapshot({"id": "k", "name": "A", "kind": "Gene", field: wrong})

    def test_snapshot_rejects_invalid_text_and_level_fields_cleanly(self):
        cases = [("definition", value) for value in (None, [], {}, 4)]
        cases += [("name", value) for value in (None, [], {}, " ")]
        cases += [("kind", value) for value in (None, [], {}, "unrecognized")]
        cases += [("entity_level", value) for value in (None, [], {}, "entity", "CLASS")]
        for field, wrong in cases:
            with self.subTest(field=field, wrong=wrong), self.assertRaises(ValueError):
                self._snapshot({"id": "k", "name": "A", "kind": "Gene", field: wrong})

    def test_snapshot_rejects_invalid_metadata_cleanly(self):
        with self.assertRaisesRegex(ValueError, "metadata must be an object"):
            self._snapshot({"id": "k", "name": "A", "kind": "Gene"}, metadata=[])

    def test_imported_ids_are_namespaced_and_gene_products_are_not_identity(self):
        ids = extract_standard_ids({
            "primary_external_id": "123", "gene_id": "NCBIGene:2",
            "ensembl_gene_ids": "ENSG00000175899",
            "external_ids_json": json.dumps({"NCBI Gene": "2", "UniProt": ["P01023"],
                                               "protein_ids_from_disgenet": ["P01023"]}),
        }, "Gene")
        self.assertEqual(ids, ["ENSEMBL:ENSG00000175899", "NCBIGene:2"])
        self.assertEqual(extract_standard_ids({"primary_external_id": "123"}, "Gene"), [])
        self.assertEqual(extract_standard_ids({"disease_id": "MENTION:Disease_NAFLD"}, "Disease"), [])
        disease = extract_standard_ids({"external_ids": "MONDO_0013209;DO_0080208;MESH_D065626;UMLS_C0400966"}, "Disease")
        self.assertEqual(disease, ["DOID:0080208", "MONDO:0013209", "MeSH:D065626", "UMLS:C0400966"])
        self.assertEqual(extract_standard_ids({"pathway_id": "KEGG:hsa00010"}, "Pathway"), ["KEGG_PATHWAY:hsa00010"])
        self.assertEqual(extract_standard_ids({"pr_id": "PR:P12345"}, "Protein"), ["PR:P12345"])
        self.assertEqual(extract_standard_ids({"go_id": "GO:0000001"}, "Pathway"), ["GO:0000001"])

    def test_annotation_and_species_preserved_without_inventing_individual_level(self):
        node = normalize_kg_node({"element_id": "4:db:7", "labels": ["Protein"], "properties": {
            "name": "FAS", "annotation": "Fas cell surface death receptor", "protein_id": "9606.ENSP00000347979",
            "ncbi_taxon_id": 9606, "species_name": "Homo sapiens", "class": "enzyme",
        }}, database="test-db")
        self.assertEqual(node["definition"], "Fas cell surface death receptor")
        self.assertEqual(node["standard_ids"], ["STRING:9606.ENSP00000347979"])
        self.assertEqual(node["entity_level"], "unknown")
        self.assertEqual(node["ncbi_taxon_id"], 9606)
        self.assertEqual(node["taxon_id"], "9606")
        self.assertEqual(node["source_context"]["class"], "enzyme")
        self.assertEqual(node["source"]["database"], "test-db")
        self.assertEqual(node["source"]["properties"]["class"], "enzyme")

    def test_existing_graph_not_unplaced_source_concepts_is_loaded(self):
        graph = {"nodes": [{"id": "type:Disease", "kind": "Disease", "name": "Disease", "origin": "schema_root"},
                           {"id": "a", "kind": "Disease", "name": "Alpha", "aliases": ["A"], "ncbi_taxon_id": 9606, "species_name": "Homo sapiens"}],
                 "edges": [{"child_id": "a", "parent_id": "type:Disease", "relation": "is_a"}],
                 "source_concepts": [{"id": "unplaced", "kind": "Disease", "name": "Beta"}]}
        with TemporaryDirectory() as temp:
            path = Path(temp) / "placement.json"
            path.write_text(json.dumps(graph))
            before = path.read_bytes()
            result = load_ontology(path)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(result["nodes"]), 2)
        self.assertEqual(result["nodes"][1]["parent_ids"], ["type:Disease"])
        self.assertTrue(result["nodes"][0]["is_root"])
        self.assertEqual(result["nodes"][1]["taxon_id"], "9606")
        self.assertEqual(result["nodes"][1]["species"], "Homo sapiens")
        self.assertEqual(len(result["metadata"]["source_file"]["sha256"]), 64)

    def test_dangling_edges_rejected(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "placement.json"
            path.write_text(json.dumps({"nodes": [], "edges": [{"child_id": "a", "parent_id": "b"}]}))
            with self.assertRaisesRegex(ValueError, "absent"):
                load_ontology(path)

    def test_read_access_fixed_queries_only(self):
        from neo4j import READ_ACCESS
        driver = MagicMock()
        driver.__enter__.return_value = driver
        session = driver.session.return_value.__enter__.return_value
        tx = MagicMock()
        seen = []
        def run(query, **parameters):
            seen.append((query, parameters))
            response = MagicMock()
            if query == COUNTS_QUERY:
                response.data.return_value = [{"labels": ["Gene"], "count": 1}]
            elif query == SCHEMA_QUERY:
                response.data.return_value = [{"labels": ["Gene"], "property_keys": ["name"]}]
            elif query == RELATION_COUNT_QUERY:
                response.single.return_value = {"count": 0}
            elif query == SAMPLE_QUERY:
                response.data.return_value = [{"element_id": "4:db:1", "labels": ["Gene"], "properties": {"name": "A", "gene_id": "NCBIGene:1"}}]
            else:
                self.fail("Unexpected database query")
            return response
        tx.run.side_effect = run
        session.execute_read.side_effect = lambda callback: callback(tx)
        with patch("entity_ontology.alignment.loading._driver", return_value=driver):
            snapshot = read_neo4j("bolt://test", "reader", "secret", types=["Gene"], limit_per_type=2)
        driver.session.assert_called_once_with(database="neo4j", default_access_mode=READ_ACCESS)
        self.assertEqual(len(snapshot["nodes"]), 1)
        self.assertEqual(len(seen), 4)
        self.assertNotIn("secret", json.dumps(snapshot))
        self.assertEqual(seen[-1][1], {"kind": "Gene", "limit": 2})


if __name__ == "__main__":
    unittest.main()
