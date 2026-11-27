import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from entity_ontology.backbone import BackbonePlan, apply_v2_plan, build_v2_plan
from entity_ontology.linking.kg_links import DEFAULT_V2_DATA, V2_FILES, gene_key
from liverkg_cli.app import app


class BackbonePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = build_v2_plan(DEFAULT_V2_DATA)

    def test_frozen_inputs_and_shared_keys(self):
        report = self.plan.report()
        self.assertEqual(set(report["inputs"]), set(V2_FILES))
        self.assertTrue(all(len(info["sha256"]) == 64 for info in report["inputs"].values()))
        self.assertEqual(report["plan"]["node_count"], len(self.plan.nodes))
        self.assertEqual(report["plan"]["relationship_count"], len(self.plan.relationships))
        self.assertIn(("Gene", gene_key("2")), self.plan.nodes)
        for properties in self.plan.nodes.values():
            self.assertTrue(properties["source"])
            self.assertTrue(properties["source_version"].startswith("v2:sha256:"))

    def test_cohort_records_keep_their_individual_ids(self):
        table = self.plan.tables["lihc_prognostic"]
        ids = {row["relationship_id"] for row in table.rows}
        imported = {
            record_id: relation for (predicate, _, record_id), relation in self.plan.relationships.items()
            if predicate == "PROGNOSTIC_IN"
        }
        self.assertEqual(set(imported), ids)
        self.assertLess(len({(edge.start, edge.end) for edge in imported.values()}), len(imported))
        for edge in imported.values():
            self.assertIsInstance(edge.properties["p_value"], float)
            self.assertEqual(edge.properties["evidence_class"], "direct_disease_evidence")
            self.assertIn(edge.properties["cohort"], {"tcga", "validation"})

    def test_unmapped_string_rows_do_not_create_proteins_or_edges(self):
        mapped = []
        rows = self.plan.tables["string_mapping"].rows
        for row in rows:
            if row["mapping_status"] == "mapped":
                mapped.append((row["gene_id"], row["string_protein_id"]))
        self.assertEqual(self.plan.unmapped_string_genes, len(rows) - len(mapped))
        self.assertEqual(
            {node_id for label, node_id in self.plan.nodes if label == "Protein"},
            {protein for _, protein in mapped},
        )
        self.assertEqual(
            {(edge.start[1], edge.end[1]) for (predicate, _, _), edge in self.plan.relationships.items()
             if predicate == "ENCODES"},
            {(gene_key(gene), protein) for gene, protein in mapped},
        )

    def test_progression_uses_the_ordered_disease_rows(self):
        stages = sorted(
            (props["stage_order"], node_id)
            for (label, node_id), props in self.plan.nodes.items() if label == "Disease"
        )
        self.assertEqual(
            {(edge.start[1], edge.end[1]) for (predicate, _, _), edge in self.plan.relationships.items()
             if predicate == "PROGRESSES_TO"},
            {(start[1], end[1]) for start, end in zip(stages, stages[1:])},
        )

    def test_missing_endpoint_is_rejected_before_import(self):
        plan = BackbonePlan({})
        plan.add_node("Gene", {"gene_id": gene_key("2")})
        with self.assertRaisesRegex(ValueError, "Unknown Disease"):
            plan.add_relationship(
                ("ASSOCIATED_WITH", "Gene", "Disease"), gene_key("2"), "unmapped",
                {"relation_id": "source-record"},
            )

    def test_reused_relation_id_with_conflicting_endpoint_is_rejected(self):
        plan = BackbonePlan({})
        plan.add_node("Gene", {"gene_id": gene_key("2")})
        plan.add_node("Disease", {"disease_id": "UMLS:first"})
        plan.add_node("Disease", {"disease_id": "UMLS:second"})
        signature = ("ASSOCIATED_WITH", "Gene", "Disease")
        properties = {"relation_id": "source-record"}
        plan.add_relationship(signature, gene_key("2"), "UMLS:first", properties)
        with self.assertRaisesRegex(ValueError, "Conflicting ASSOCIATED_WITH"):
            plan.add_relationship(signature, gene_key("2"), "UMLS:second", properties)

    def test_remote_protected_and_routed_targets_are_rejected(self):
        with patch("neo4j.GraphDatabase.driver") as driver:
            for uri, database in (
                ("bolt://example.invalid:7687", "neo4j"),
                ("bolt://127.0.0.1:7687", "liver-kg-core-v02"),
                ("bolt://127.0.0.1:7687", None),
                ("neo4j://localhost:7687", "neo4j"),
            ):
                with self.subTest(uri=uri, database=database), self.assertRaises(ValueError):
                    apply_v2_plan(self.plan, uri=uri, database=database)
            driver.assert_not_called()

    def test_dry_run_is_the_normal_entry_point_and_never_connects(self):
        with TemporaryDirectory() as directory, patch("neo4j.GraphDatabase.driver") as driver:
            report = Path(directory) / "preview.json"
            result = CliRunner().invoke(app, ["backbone", "build", "--report", str(report)])
            self.assertEqual(result.exit_code, 0, result.output)
            preview = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(preview["mode"], "dry-run")
            self.assertEqual(preview, json.loads(result.output))
            self.assertEqual(preview["plan"], self.plan.report()["plan"])
            driver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
