"""Exercise frozen coverage and stratified KG input loading."""

from collections import Counter
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from entity_ontology.enrichment.inputs import load_enrichment_inputs
from entity_ontology.enrichment.obo import OBOCatalog
from entity_ontology.storage import digest


def kg_node(number, kind):
    node_id = f"4:db:{number}"
    return {
        "id": node_id,
        "kind": kind,
        "name": f"Entity {number}",
        "standard_ids": [f"NCBIGene:{number}"] if kind == "Gene" else [],
        "aliases": [f"Alias {number}"],
        "definition": f"Source definition for entity {number}.",
        "source": {"database": "example", "element_id": node_id, "properties": {
            "source": "Test registry", "source_record_id": str(number)
        }},
    }


def assessment(node, **fields):
    return {
        "kg_node_id": node["id"],
        "kg_name": node["name"],
        "entity_type": node["kind"],
        "decision_status": "UNMATCHED",
        "relation_type": "UNMATCHED",
        **fields,
    }


class EnrichmentInputTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.nodes = [
            kg_node(i, kind)
            for i, kind in enumerate(["Disease"] * 4 + ["Gene"] * 4 + ["Tissue"] * 3)
        ]
        self.rows = [assessment(node) for node in self.nodes]
        self.rows[0] = assessment(self.nodes[0], decision_status="ALIGNED", relation_type="MAPS_TO",
                                  ontology_node_id="disease", evidence={"record": "same disease"})
        self.rows[1]["decision_status"] = "REVIEW_REQUIRED"
        self.category_edge = assessment(self.nodes[4], decision_status="ALIGNED",
                                        relation_type="FUNCTIONAL_CLASS_MEMBERSHIP",
                                        ontology_node_id="gene-class", evidence={"record": "category"})
        self.screened = assessment(self.nodes[5], grounded_external_ids=["NCBIGene:5"],
                                   screening="registry_exact")
        self.snapshot = self._write("snapshot.json", {"nodes": self.nodes, "metadata": {"database": "example"}})
        self.alignment = self._write("alignment.json", {"rows": self.rows})
        self.categories = self._write("categories.json", {"confirmed": [self.category_edge],
                                                          "screened_rows": [self.screened]})
        self.audit = self._write("audit.json", {"rows": [], "secondary_edges": [self.category_edge]})

    def _write(self, name, artifact):
        path = self.directory / name
        path.write_text(json.dumps(artifact), encoding="utf-8")
        return path

    def _load(self, size=6):
        return load_enrichment_inputs(self.snapshot, [self.alignment, self.categories, self.audit],
                                      sample_size=size, seed=123)

    def test_coverage_union_and_duplicate_edge_provenance(self):
        result = self._load()
        baseline = result["baseline"]
        self.assertEqual(set(baseline["covered_ids"]), {self.nodes[i]["id"] for i in (0, 4)})
        self.assertEqual(baseline["identity_ids"], [self.nodes[0]["id"]])
        self.assertEqual(len(baseline["rows"]), 2)
        category = next(row for row in baseline["rows"] if row["relation_type"] != "MAPS_TO")
        self.assertEqual([item["json_pointer"] for item in category["artifact_records"]],
                         ["/confirmed/0", "/secondary_edges/0"])
        self.assertTrue(set(baseline["covered_ids"]).isdisjoint(n["id"] for n in result["sample"]))

    def test_balanced_sample_reproducible_across_source_order_and_preserves_evidence(self):
        before = self.snapshot.read_bytes()
        first = self._load()
        self.assertEqual(self.snapshot.read_bytes(), before)
        self.assertEqual(Counter(node["kind"] for node in first["sample"]),
                         {"Disease": 2, "Gene": 2, "Tissue": 2})
        self._write("snapshot.json", {"nodes": list(reversed(self.nodes))})
        second = self._load()
        self.assertEqual([node["id"] for node in first["sample"]], [node["id"] for node in second["sample"]])
        all_uncovered = self._load(size=9)["sample"]
        self.assertIn(self.nodes[1]["id"], {node["id"] for node in all_uncovered})
        chosen = next(node for node in all_uncovered if node["id"] == self.nodes[5]["id"])
        self.assertEqual(chosen["source"], self.nodes[5]["source"])
        self.assertEqual(chosen["definition"], self.nodes[5]["definition"])
        self.assertEqual(chosen["standard_ids"], self.nodes[5]["standard_ids"])
        self.assertEqual(chosen["aliases"], self.nodes[5]["aliases"])
        self.assertEqual(chosen["kg_record"]["json_pointer"], "/nodes/5")
        self.assertEqual(chosen["prior_grounding"][0]["artifact_record"]["json_pointer"], "/screened_rows/0")

    def test_stops_on_snapshot_alignment_mismatch(self):
        self.rows[-1]["kg_node_id"] = "4:different-database:10"
        self._write("alignment.json", {"rows": self.rows})
        with self.assertRaisesRegex(ValueError, "absent from the snapshot"):
            self._load()

    def test_stops_on_incomplete_baseline_coverage(self):
        self._write("alignment.json", {"rows": self.rows[:-1]})
        with self.assertRaisesRegex(ValueError, "cover the supplied KG snapshot"):
            self._load()

    def test_stops_on_conflicting_strict_baseline_connections(self):
        conflicting = {**self.rows[0], "ontology_node_id": "different-target"}
        self._write("audit.json", {"rows": [conflicting]})
        with self.assertRaisesRegex(ValueError, "Conflicting strict alignment"):
            self._load()

    def test_signed_cohort_preserves_small_sample_and_exact_order(self):
        metadata = self._load()["metadata"]
        entries = [{"id": self.nodes[i]["id"], "kind": self.nodes[i]["kind"]} for i in (6, 2)]
        document = {
            "version": "enrichment-frozen-cohort-v1", "cohort": "unseen",
            "selection": {"method": "fixture"}, "nodes": entries, "baseline_ontology": {},
            "kg_snapshot": metadata["kg_snapshot"]["snapshot_file"],
            "alignment_files": metadata["alignment_files"],
        }
        artifact = {**document, "signature": digest(document)}
        cohort = self._write("cohort.json", artifact)
        paths = [self.alignment, self.categories, self.audit]
        loaded = load_enrichment_inputs(self.snapshot, paths, cohort_path=cohort)
        self.assertEqual([n["id"] for n in loaded["sample"]], [e["id"] for e in entries])
        self.assertEqual(loaded["metadata"]["sampling"]["cohort_signature"], artifact["signature"])

        self._write("cohort.json", {**artifact, "nodes": list(reversed(entries))})
        with self.assertRaisesRegex(ValueError, "Invalid or changed enrichment cohort"):
            load_enrichment_inputs(self.snapshot, paths, cohort_path=cohort)


class OBOSourceTests(unittest.TestCase):
    def _catalog(self, records):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "cell.obo"
        path.write_text("format-version: 1.2\ndata-version: test-release\n" + records, encoding="utf-8")
        return OBOCatalog({"CellType": path})

    def test_only_active_exact_terms_supply_definition_and_is_a(self):
        catalog = self._catalog("""
[Term]
id: CL:1
name: respiratory type 1 cell
def: "A particular respiratory cell." [PMID:123]
synonym: "type I respiratory cells" EXACT []
synonym: "generic lung cell" BROAD []
is_a: CL:2 ! epithelial cell
relationship: BFO:0000050 UBERON:4 ! part of an organ

[Term]
id: CL:2
name: epithelial cell
def: "A parent cell class." []

[Term]
id: CL:3
name: inactive record
synonym: "type I respiratory cells" EXACT []
is_obsolete: true
""")
        node = {"kind": "CellType", "name": "respiratory cells type 1"}
        match = catalog.lookup(node)
        self.assertEqual(match["record_id"], "CL:1")
        self.assertEqual(match["definition"], "A particular respiratory cell.")
        self.assertEqual(match["direct_parent_ids"], ["CL:2"])
        self.assertEqual(match["parent_records"][0]["label"], "epithelial cell")
        self.assertEqual(match["artifact"]["release"], "test-release")
        self.assertEqual(match["match"], "normalized_cell_label_or_synonym")
        self.assertIsNone(catalog.lookup({**node, "name": "generic lung cell"}))
        self.assertIsNone(catalog.lookup({**node, "standard_ids": ["CL:9"]}))

    def test_ambiguous_name_cannot_ground_a_definition(self):
        catalog = self._catalog("""
[Term]
id: CL:1
name: first concept
def: "The first class." []
synonym: "shared cell" EXACT []

[Term]
id: CL:2
name: second concept
def: "The second class." []
synonym: "shared cells" EXACT []
""")
        self.assertIsNone(catalog.lookup({"kind": "CellType", "name": "shared cell"}))


if __name__ == "__main__":
    unittest.main()
