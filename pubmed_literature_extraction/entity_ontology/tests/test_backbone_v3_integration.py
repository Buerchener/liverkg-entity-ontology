import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from cognitive_agent.schema.write_contract import METABOLITE_DISEASE

from entity_ontology.backbone import BackbonePlan
from entity_ontology.backbone_schema import V3_NODE_KEYS, V3_RELATION_KEYS
from entity_ontology.backbone_snapshot import export_snapshot
from entity_ontology.export.graph import GRAPH_FILES, VERSION
from entity_ontology.export.neo4j_load import load_plan, load_run
from entity_ontology.export.neo4j_v3 import statements_v3
from entity_ontology.linking.kg_links import build_index, link_concept, backbone_identity
from entity_ontology.relations.project import novelty
from entity_ontology.storage import atomic_json, atomic_jsonl


def make_snapshot(folder):
    plan = BackbonePlan({}, version="v3", node_keys=V3_NODE_KEYS, relationship_keys=V3_RELATION_KEYS)
    for label, identifier, name, extras in (
        ("Metabolite", "HMDB:HMDB0001", "Cholate", {"xrefs": ["CHEBI:1", "KEGG.COMPOUND:C00001"]}),
        ("Metabolite", "HMDB:HMDB0002", "Glycocholate", {"xrefs": ["CHEBI:1"]}),
        ("Disease", "UMLS:1", "NAFLD", {"aliases": ["MASLD"], "related_aliases": ["MetALD"], "stage_order": 1}),
    ):
        plan.add_node(label, {V3_NODE_KEYS[label]: identifier, "name": name, **extras})
    plan.add_relationship((METABOLITE_DISEASE, "Metabolite", "Disease"), "HMDB:HMDB0001", "UMLS:1",
                          {"relationship_id": "rel", "source": "HMDB"})
    return export_snapshot(plan, folder)


def make_graph(folder, predicate=METABOLITE_DISEASE):
    plan = {name: [] for name in GRAPH_FILES}
    plan["articles"] = [{"pmid": "1", "title": "Fixture"}]
    plan["concepts"] = [{"uid": "run/m", "kind": "Metabolite", "run_id": "run"},
                        {"uid": "run/d", "kind": "Disease", "run_id": "run"}]
    plan["same_as"] = [
        {"concept_uid": "run/m", "label": "Metabolite", "key": "metabolite_id", "name": "Cholate", "node_id": "HMDB:HMDB0001"},
        {"concept_uid": "run/d", "label": "Disease", "key": "disease_id", "name": "NAFLD", "node_id": "UMLS:1"},
    ]
    plan["relations"] = [{"uid": "run/r", "predicate": predicate, "run_id": "run",
                          "subject_uid": "run/m", "object_uid": "run/d", "gate": "AUTO", "stage_order": 1}]
    plan["evidence"] = [{"uid": "run/e", "run_id": "run", "pmid": "1", "relation_uid": "run/r",
                         "subject_uid": "run/m", "object_uid": "run/d", "text": "A source sentence."}]
    files = {}
    for name, rows in plan.items():
        path = folder / "graph" / f"{name}.jsonl"
        atomic_jsonl(path, rows)
        files[name] = {"rows": len(rows), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest = {"version": VERSION, "run_id": "run", "files": files,
                "backbone": {"version": "v3", "backbone_snapshot_id": "a" * 64}}
    atomic_json(folder / "graph/manifest.json", manifest)


class V3LinkingAndLoaderTests(TestCase):
    def test_exact_ids_related_terms_and_four_novelty_states(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = make_snapshot(root)
            index = build_index(root)
            self.assertEqual(backbone_identity(root), {"backbone_snapshot_id": manifest["snapshot_id"]})
            compound = link_concept({"id": "m", "kind": "Metabolite", "name": "Cholate"}, index, ["cpd:C00001"])
            disease = link_concept({"id": "d", "kind": "Disease", "name": "MASLD"}, index, [])
            other = link_concept({"id": "o", "kind": "Metabolite", "name": "Glycocholate"}, index, [])
            self.assertEqual((compound["status"], disease["status"]), ("LINKED", "LINKED"))
            self.assertEqual(disease["target"]["stage_order"], 1)
            self.assertEqual(link_concept({"id": "a", "kind": "Disease", "name": "MetALD"}, index, [])["status"], "AMBIGUOUS")
            self.assertEqual(link_concept({"id": "a", "kind": "Metabolite", "name": "Other"}, index, ["ChEBI:1"])["status"], "AMBIGUOUS")
            relation = {"subject_concept_id": "m", "object_concept_id": "d", "predicate": METABOLITE_DISEASE}
            links = {"m": compound, "d": disease, "o": other}
            self.assertEqual(novelty(relation, links, index)[0], "KNOWN_IN_BACKBONE")
            self.assertEqual(novelty({**relation, "predicate": "OTHER"}, links, index)[0], "PAIR_KNOWN_OTHER_RELATION")
            self.assertEqual(novelty({**relation, "subject_concept_id": "o"}, links, index)[0], "NEW")
            self.assertEqual(novelty({**relation, "subject_concept_id": "missing"}, links, index)[0], "UNLINKED")
            with (root / "nodes.jsonl").open("a", encoding="utf-8") as stream:
                stream.write("\n")
            with self.assertRaisesRegex(ValueError, "snapshot manifest"):
                build_index(root)

    def test_v3_validates_and_matches_real_nodes_for_new_predicate(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            make_graph(folder)
            plan = load_plan(folder)
            calls = list(statements_v3(plan))
            same_as = [query for name, query, rows in calls if name.startswith("same_as:")]
            self.assertTrue(all("MATCH (n:" in query and "V2Anchor" not in query for query in same_as))
            direct = next((query, rows) for name, query, rows in calls if name.startswith("backbone_relations:"))
            self.assertIn(f"r:{METABOLITE_DISEASE} {{relationship_id:", direct[0])
            self.assertEqual(direct[1][0]["props"]["stage_order"], 1)
            driver = mock.Mock()
            summary = load_run(folder, uri="bolt://127.0.0.1:17687", database="neo4j", user="neo4j",
                               password="", dry_run=True, driver_factory=driver)
            self.assertEqual(summary["backbone"]["version"], "v3")
            driver.assert_not_called()
            make_graph(folder, predicate="ASSOCIATED_WITH")
            with self.assertRaisesRegex(ValueError, "Unsupported v3"):
                load_plan(folder)
