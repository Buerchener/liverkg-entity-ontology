"""Exercise the production pipeline with synthetic provider and extractor responses."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from entity_ontology import model, pipeline
from entity_ontology.concepts.deduplicate import (
    indexed_equivalence, recheck_mention_types, review_and_merge, single_missing_pairs,
)
from entity_ontology.concepts.embedding import EmbeddingAPI
from entity_ontology.concepts.normalization import classify_output
from entity_ontology.hierarchy.entity_placement_dag import EntityDAG
from entity_ontology.model import endpoint, read_settings
from entity_ontology.storage import atomic_json, read_json


def fake_extract(articles, output, *, config):
    article = json.loads(articles.read_text())
    text = "TITLE: " + article["title"] + "\nABSTRACT: " + article["abstract"]
    units = []
    for index, fragment in enumerate(article["abstract"].split(". "), 1):
        sentence = fragment if fragment.endswith(".") else fragment + "."
        start = text.index(sentence)
        units.append({
            "unit_id": f"u{index}", "parent_sentence_id": f"s{index}", "section": "ABSTRACT",
            "text": sentence, "char_start": start, "char_end": start + len(sentence),
        })
    entities = []
    for name in ("Beta protein", "BP", "Dual protein"):
        start = text.index(name)
        entities.append({
            "mention": name, "type": "Protein", "source_span": name, "char_start": start,
            "char_end": start + len(name), "grounded": True, "alignment_status": "PARSED",
        })
    artifact = output / "agent_results.json"
    atomic_json(artifact, {
        "report": {"dry_run": True, "neo4j_connected": False, "total_articles": 1, "model": "fixture"},
        "records": [{"pmid": article["pmid"], "phases": {
            "reader": {"units": units}, "extraction": {"entities": entities},
        }}],
    })
    return artifact


def fake_embeddings(texts):
    vectors = {"Alpha protein": [1, 0, 0], "Beta protein": [0, 1, 0], "Dual protein": [0, 0, 1]}
    return {"vectors": [vectors[text.splitlines()[0]] for text in texts], "model": "fixture"}


class FixtureProvider:
    def generate(self, stage, prompt, payload, parameters):
        if stage == "global_concept_rank":
            concepts = sorted(payload["concepts"], key=lambda row: row["name"])
            result = {"order": [row["id"] for row in concepts]}
        else:
            result = {"rows": [self.answer(stage, item) for item in payload["items"]]}
        return {
            "content": json.dumps(result), "finish_reason": "stop",
            "model": parameters["model"], "usage": {},
        }

    def answer(self, stage, item):
        row = {"id": item["id"]}
        if stage == "entity_coverage_v1":
            return {**row, "additions": [
                {"mention": "Alpha protein", "kind": "Protein"},
                {"mention": "Invented protein", "kind": "Protein"},
            ]}
        if stage == "normalize_referent_v2":
            name = "Beta protein" if item["mention"] == "BP" else item["mention"]
            return {**row, "name": name, "kind": "Protein", "definition": f"Class {name}.",
                    "aliases": ["BP"] if name == "Beta protein" else [], "reason": "fixture"}
        if stage == "normalize_type_check_v2":
            return {**row, "kind": "Protein"}
        if stage == "equivalence":
            same = item["left"]["name"] == item["right"]["name"]
            return {**row, "verdict": "equivalent" if same else "distinct", "reason": "fixture"}
        if stage == "incremental_propose":
            return {**row, "new_parent": None, "comparisons": [
                {"id": candidate["id"], "a_in_b": item["name"] == "Dual protein",
                 "b_in_a": False, "reason": "fixture"}
                for candidate in item["candidates"]
            ]}
        if stage == "incremental_edge_review":
            return {**row, "supported": item["child"]["name"] == "Dual protein", "reason": "fixture"}
        raise AssertionError(f"Unexpected fixture model stage {stage}.")


class PipelineTest(unittest.TestCase):
    def test_single_placement_object_keeps_exact_target_id(self):
        with TemporaryDirectory() as directory:
            session = model.ModelSession(Path("unused"), Path(directory))
            row = {"id": "target", "comparisons": [], "new_parent": None}
            provider = Mock()
            provider.generate.return_value = {
                "content": json.dumps(row), "finish_reason": "stop", "model": "fixture", "usage": {},
            }
            with (patch.object(session, "request_settings", return_value=({"base_url": "https://fixture"}, {})),
                  patch.object(model, "live_provider", return_value=provider)):
                result = session.req(("incremental_propose", "fixture", {"items": [{"id": "target"}]}))
                self.assertEqual(result, [row])
                provider.generate.assert_called_once()
                with self.assertRaises(ValueError):
                    session.req(("incremental_propose", "fixture", {"items": [{"id": "other"}]}))

    def test_indexed_equivalence_restores_original_pair_ids(self):
        inputs = [{"id": f"12345678:M{i:02d}|87654321:M01", "left": {"name": str(i)}}
                  for i in range(23)]

        def answer(task):
            stage, _, payload = task
            self.assertEqual(stage, "equivalence_indexed")
            self.assertEqual([row["id"] for row in payload["items"]],
                             [f"p{i}" for i in range(1, len(payload["items"]) + 1)])
            return [{"id": row["id"], "verdict": "distinct", "reason": row["left"]["name"]}
                    for row in reversed(payload["items"])]

        session = Mock()
        session.req.side_effect = answer
        decisions = indexed_equivalence(inputs, session)
        self.assertEqual({r["id"]: r["reason"] for r in decisions},
                         {r["id"]: r["left"]["name"] for r in inputs})
        self.assertEqual(session.req.call_count, 3)

    def test_type_gate_and_independent_one_gap_review(self):
        kept = {n: {"name": n, "kind": "Gene" if n == "x" else "Protein", "definition": n, "aliases": []}
                for n in "abcx"}
        evidence = {n: {"mention": n, "sentence": "Sentence for " + n} for n in kept}
        pairs = [{"left": a, "right": b} for a, b in [("a", "b"), ("b", "c"), ("a", "x")]]
        for verdict in ("equivalent", "distinct", "uncertain"):
            with self.subTest(verdict=verdict):
                calls = []

                def review(items):
                    calls.append([r["id"] for r in items])
                    self.assertTrue(all(r["left"]["kind"] == r["right"]["kind"] for r in items))
                    for r in items:
                        self.assertEqual(r["left"]["sentence"], "Sentence for " + r["left"]["name"])
                    return [{"id": r["id"], "verdict": verdict if r["id"] == "a|c" else "equivalent"}
                            for r in items]

                result = review_and_merge(kept, evidence, pairs, review=review, recheck_types=self.fail)
                self.assertEqual(calls, [["a|b", "b|c"], ["a|c"]])
                votes = {r["id"]: r for r in result["pair_judgments"]}
                self.assertIsNone(votes["a|x"]["verdict"])
                self.assertEqual(votes["a|x"]["decision_source"], "type_gate")
                self.assertEqual(votes["a|c"]["decision_source"], "single_missing_pair")
                merged = [list(c["source_mentions"]) for c in result["concepts"]]
                self.assertEqual(merged, [["a", "b", "c"], ["x"]] if verdict == "equivalent"
                                 else [["a"], ["b"], ["c"], ["x"]])

    def test_shared_alias_rechecks_each_occurrence_once_before_equivalence(self):
        kept = {
            "a": {"name": "ALB", "aliases": [], "kind": "Gene", "definition": "Gene."},
            "b": {"name": "Albumin", "aliases": [" alb "], "kind": "Protein", "definition": "Protein."},
            "c": {"name": "Albumin variant", "aliases": ["Alb"], "kind": "Protein", "definition": "Variant."},
            "d": {"name": "Another gene", "aliases": [], "kind": "Gene", "definition": "Another gene."},
        }
        sentences = {"a": "Serum ALB was measured.", "b": "Albumin was purified.",
                     "c": "The albumin variant was detected.", "d": "Another locus was identified."}
        evidence = {i: {"mention": r["name"], "sentence": sentences[i]} for i, r in kept.items()}
        pairs = [{"left": "a", "right": i} for i in "bcd"]

        def recheck(items):
            self.assertEqual(items, [{"id": i, **evidence[i]} for i in "abc"])
            return [{**kept[i], "id": i, "kind": "Protein", "status": "keep",
                     "definition": "Protein from source."} for i in "cba"]

        def review(items):
            self.assertEqual([r["id"] for r in items], ["a|b", "a|c"])
            self.assertEqual(items[0]["left"]["definition"], "Protein from source.")
            self.assertEqual(items[0]["left"]["sentence"], sentences["a"])
            return [{"id": r["id"], "verdict": "equivalent" if r["id"] == "a|b" else "distinct"}
                    for r in items]

        out = review_and_merge(kept, evidence, pairs, review=review, recheck_types=recheck)
        self.assertEqual([(r["source_mentions"], r["kind"]) for r in out["concepts"]],
                         [(["a", "b"], "Protein"), (["c"], "Protein"), (["d"], "Gene")])
        self.assertEqual(out["pair_judgments"][-1]["decision_source"], "type_gate")
        self.assertEqual(out["type_rechecks"][0]["initial_kind"], "Gene")
        self.assertEqual(kept["a"]["kind"], "Gene")

    def test_matching_names_can_still_be_distinct_or_unresolved(self):
        kept = {i: {"kind": kind, "name": "Symbol", "aliases": [], "definition": kind}
                for i, kind in [("a", "Gene"), ("b", "Protein")]}
        evidence = {i: {"mention": "Symbol", "sentence": "Sentence " + i} for i in kept}
        for kind, state, verdict in [("Gene", "keep", "distinct"), ("", "review", "uncertain"),
                                     ("Measurement", "outside_scope", "uncertain")]:
            with self.subTest(kind=kind):
                recheck = Mock(return_value=[{"id": "a", **kept["a"], "kind": kind, "status": state},
                                             {"id": "b", **kept["b"], "status": "keep"}])
                out = review_and_merge(kept, evidence, [{"left": "a", "right": "b"}],
                                       review=self.fail, recheck_types=recheck)
                self.assertEqual([c["source_mentions"] for c in out["concepts"]], [["a"], ["b"]])
                self.assertEqual(out["pair_judgments"][0]["applied_verdict"], verdict)
                self.assertEqual(out["concepts"][0].get("type_review_status"),
                                 None if state == "keep" else "needs_review")

    def test_source_type_recheck_requires_a_quote_from_that_occurrence(self):
        item = {"id": "a", "mention": "ALB", "sentence": "Serum ALB was measured."}
        row = {"id": "a", "kind": "Protein", "name": "Albumin", "aliases": [],
               "definition": "Albumin protein.", "reason": "Serum measurement."}
        incorrect = {**row, "source_quote": "Some other sentence."}
        correct = {**row, "source_quote": "Serum ALB"}
        session = Mock()
        session.req.side_effect = [[incorrect], [correct]]
        result, = recheck_mention_types([item], session=session)
        self.assertEqual(result["source_quote"], "Serum ALB")
        for call in session.req.call_args_list:
            self.assertEqual(call.args[0][2]["items"], [item])
        session.req.side_effect = [[incorrect], [incorrect]]
        invalid, = recheck_mention_types([item], session=session)
        self.assertEqual((invalid["status"], invalid["review_error"]), ("review", "invalid_type_evidence"))

    def test_only_unjudged_final_pair_qualifies(self):
        decisions = {("a", "b"): {"relation": "equivalent"}, ("b", "c"): {"relation": "equivalent"}}
        for existing in ("distinct", "uncertain"):
            recorded = {**decisions, ("a", "c"): {"relation": existing}}
            self.assertEqual(single_missing_pairs([("c", "b", "a")], recorded), [])
        decisions["c", "d"] = {"relation": "equivalent"}
        self.assertEqual(single_missing_pairs([("a", "b", "c", "d")], decisions), [])

    def test_model_rationale_field_aliases_preserve_text(self):
        mention = {"id": "1:M01", "kind": "Protein", "name": "A protein",
                   "definition": "A protein class.", "aliases": []}
        for field in ("reason", "reason_cn", "reason_chn"):
            with self.subTest(field=field):
                decision = classify_output({**mention, field: " 原句明确描述了蛋白质。 "})
                self.assertEqual(decision["reason"], "原句明确描述了蛋白质。")
        with self.assertRaises(ValueError):
            classify_output({**mention, "reason_cn": " "})

    def test_grounded_coverage_through_multi_parent_placement_and_resume(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            articles = root / "articles.jsonl"
            articles.write_text(json.dumps({
                "pmid": "100000", "title": "Synthetic article",
                "abstract": "Alpha protein level rose. Beta protein and BP were stable. Dual protein remained.",
            }) + "\n")
            config = root / "config.json"
            example = Path(__file__).resolve().parents[1] / "config.example.json"
            settings = read_json(example)
            settings["embedding"]["dimensions"] = 3
            atomic_json(config, settings)
            runs = root / "runs"
            run = runs / "example"
            provider = FixtureProvider()
            with (
                patch.dict("os.environ", {"LIVERKG_API_BASE": "https://provider.example/v1"}),
                patch.object(pipeline, "extract_articles", side_effect=fake_extract) as extract,
                patch.object(model, "live_provider", return_value=provider) as language_model,
                patch("entity_ontology.concepts.retrieve_pairs.live_provider", return_value=provider),
                patch.object(EmbeddingAPI, "embed", staticmethod(fake_embeddings)),
            ):
                connection, _ = read_settings(config, "audit_edges")
                self.assertEqual(endpoint(connection), endpoint(settings["embedding"]))
                options = {"config": config, "runs_dir": runs, "go_reference": False}
                result = pipeline.run_batch(articles, "example", **options)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["counts"]["coverage_mentions"], 1)
                self.assertEqual(result["counts"]["concepts"], 3)
                self.assertEqual(result["counts"]["edges"], 4)

                mentions = read_json(run / "input_mentions.json")["mentions"]
                self.assertEqual([row["mention"] for row in mentions],
                                 ["Beta protein", "BP", "Dual protein", "Alpha protein"])
                added = mentions[-1]
                self.assertEqual(added["offset_status"], "exact")
                self.assertEqual(added["upstream_status"], "ontology_coverage")
                supplement = read_json(run / "entity_coverage.json")["records"][0]
                self.assertEqual([row["reason"] for row in supplement["rejected"]], ["not_an_exact_unique_span"])

                placement_path = run / "incremental-placement/placement.json"
                placement = read_json(placement_path)
                ids = {node["name"]: node["id"] for node in placement["nodes"]}
                beta = next(node for node in placement["nodes"] if node["id"] == ids["Beta protein"])
                self.assertEqual({"100000:M01", "100000:M02"}, set(beta["source_mentions"]))
                direct_parents = {edge["parent_id"] for edge in placement["edges"]
                                  if edge["child_id"] == ids["Dual protein"]}
                self.assertEqual(direct_parents, {ids["Alpha protein"], ids["Beta protein"]})
                before = language_model.call_count
                resumed = pipeline.run_batch(articles, "example", **options)
                self.assertEqual(resumed, result)
                self.assertEqual(language_model.call_count, before)
                extract.assert_called_once()

                atomic_json(run / "manifest.json", {**result, "status": "failed"})
                with patch.object(pipeline, "EQUIVALENCE_POLICY", "another-policy"):
                    with self.assertRaisesRegex(ValueError, "different deduplication policy"):
                        pipeline.run_batch(articles, "example", **options)
                self.assertEqual(read_json(placement_path), placement)
                atomic_json(run / "manifest.json", result)
                placement_path.write_text("{}")
                with self.assertRaisesRegex(ValueError, "completed batch files"):
                    pipeline.run_batch(articles, "example", **options)

    def test_cycle_does_not_change_placed_graph(self):
        dag = EntityDAG(["Protein"])
        for ident, parents in (("a", []), ("b", ["a"])):
            dag.place({"id": ident, "kind": "Protein", "name": ident, "definition": ident}, parent_ids=parents)
        original_edges = dag.edges.copy()
        rejected = dag.place({"id": "c", "kind": "Protein", "name": "c", "definition": "c"},
                             parent_ids=["b"], child_ids=["a"])
        self.assertEqual(rejected["action"], "rejected")
        self.assertIn("cycle", rejected["reason"])
        self.assertEqual(dag.edges, original_edges)
        self.assertNotIn("c", dag.nodes)
