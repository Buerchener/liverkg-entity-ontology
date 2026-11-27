"""Exercise three independent model roles without network requests."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from entity_ontology import model, pipeline
from entity_ontology.backbone import BackbonePlan
from entity_ontology.backbone_schema import V3_NODE_KEYS, V3_RELATION_KEYS
from entity_ontology.backbone_snapshot import export_snapshot
from entity_ontology.concepts.embedding import EmbeddingAPI
from entity_ontology.extraction.mentions import read_articles
from entity_ontology.extraction.native import extractor_env
from entity_ontology.storage import atomic_json, atomic_jsonl, read_json

from test_pipeline import FixtureProvider, fake_embeddings, fake_extract


MAIN = "https://main.example/v1"
EXTRACTION = "http://127.0.0.1:18631/v1"
VECTOR = "https://embedding.example/compatible-mode/v1"
VARIABLES = {
    "LIVERKG_MAIN_API_BASE": MAIN, "LIVERKG_MAIN_API_KEY": "fixture-main",
    "LIVERKG_EXTRACT_API_BASE": EXTRACTION, "LIVERKG_EXTRACT_API_KEY": "fixture-extraction",
    "LIVERKG_EMBED_API_BASE": VECTOR, "LIVERKG_EMBED_API_KEY": "fixture-vector",
    "DEEPSEEK_API_KEY": "stale-env-value",
}


def make_settings(root):
    example = Path(__file__).resolve().parents[1] / "config.three-model.example.json"
    settings = read_json(example)
    settings["embedding"]["dimensions"] = 3
    config = root / "config.json"
    atomic_json(config, settings)
    return config


def protein_snapshot(folder):
    plan = BackbonePlan({}, version="v3", node_keys=V3_NODE_KEYS, relationship_keys=V3_RELATION_KEYS)
    for name, identifier in (("Beta protein", "9606.BETA"), ("Dual protein", "9606.DUAL")):
        plan.add_node("Protein", {"name": name, "protein_id": identifier})
    plan.add_relationship(("INTERACTS_WITH", "Protein", "Protein"), "9606.BETA", "9606.DUAL",
                          {"interaction_id": "fixture-1", "source": "STRING"})
    return export_snapshot(plan, folder)


class ThreeModelTests(TestCase):
    def test_reader_handles_unicode_paragraph_separator_inside_abstract(self):
        row = {"pmid": "42", "title": "Fixture", "abstract": "A paragraph.\u2028A second paragraph."}
        next_row = {**row, "pmid": "43"}
        data = "\n".join(json.dumps(item, ensure_ascii=False) for item in (row, next_row)).encode()
        self.assertEqual(read_articles(data), [row, next_row])

    def test_extraction_and_reasoning_use_their_respective_credentials(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, VARIABLES, clear=True):
            root = Path(directory)
            config = make_settings(root)
            run = root / "runtime"
            run.mkdir()
            env = extractor_env(run, root, config, run / "native", None)
            self.assertEqual((env["GEMINI_MODEL"], env["GEMINI_API_BASE"], env["GEMINI_API_KEY"]),
                             ("gemini-3.8-flash", EXTRACTION, "fixture-extraction"))
            for variable in ("SECOND_LLM_API_BASE", "DEEPSEEK_API_BASE", "ALIYUN_MAAS_API_BASE"):
                self.assertEqual(env[variable], MAIN)
            for variable in ("SECOND_LLM_API_KEY", "DEEPSEEK_API_KEY", "ALIYUN_MAAS_API_KEY"):
                self.assertEqual(env[variable], "fixture-main")
            self.assertEqual(env["SECOND_LLM_MODEL_ID"], "deepseek-v4.1-flash")
            self.assertEqual(env["AUX_CRITIC_MODEL"], "deepseek-v4.1-flash")
            self.assertIn("max_workers = 1", (run / "extractor.toml").read_text())

    def test_entire_pipeline_uses_three_model_settings_and_v3_snapshot(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, VARIABLES, clear=True):
            root = Path(directory)
            articles = root / "articles.jsonl"
            atomic_jsonl(articles, [{
                "pmid": "100000", "title": "Synthetic article",
                "abstract": "Alpha protein level rose. Beta protein and BP were stable. Dual protein remained.",
            }])
            snapshot = root / "offline-v3"
            manifest = protein_snapshot(snapshot)
            config = make_settings(root)
            runs = root / "runs"

            def extract(infile, output, *, config):
                result = fake_extract(infile, output, config=config)
                data = read_json(result)
                data["report"]["model"] = "gemini-3.8-flash"
                atomic_json(result, data)
                return result

            def resolve_chat(config, settings):
                self.assertEqual(model.endpoint(config), MAIN)
                self.assertEqual(settings["model"], "deepseek-v4.1-flash")
                return FixtureProvider()

            def embed(provider, texts):
                self.assertEqual(provider.config["base_url"], VECTOR)
                self.assertEqual(provider.config["api_key_env"], "LIVERKG_EMBED_API_KEY")
                self.assertEqual(provider.config["model"], "qwen3.7-text-embedding")
                return {**fake_embeddings(texts), "model": "qwen3.7-text-embedding"}

            with (
                patch.object(pipeline, "extract_articles", side_effect=extract),
                patch.object(model, "live_provider", side_effect=resolve_chat),
                patch.object(EmbeddingAPI, "embed", embed),
            ):
                result = pipeline.run_batch(articles, "v3-mixed-fixture", config=config, runs_dir=runs,
                                            entity_reference=False, v2_data=snapshot)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(len(result["stages"]), 9)
            self.assertTrue(all(value == "complete" for value in result["stages"].values()))
            self.assertEqual(result["model_roles"], {
                "main": "deepseek-v4.1-flash", "langextract": "gemini-3.8-flash", "embedding": "qwen3.7-text-embedding",
            })
            self.assertEqual(result["backbone_snapshot_id"], manifest["snapshot_id"])
            self.assertEqual(result["counts"]["relation_novelty"]["KNOWN_IN_BACKBONE"], 1)
