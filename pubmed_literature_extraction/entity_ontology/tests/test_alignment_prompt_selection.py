"""Prompt selection must preserve OLS policy and separate durable run identities."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from entity_ontology.alignment.__main__ import main
from entity_ontology.alignment.decisions import POLICY
from entity_ontology.alignment.pipeline import AlignmentModel, OLS_POLICY, run_alignment
from entity_ontology.alignment.prompts import DEFAULT_PROMPT, PROMPT_VARIANTS
from entity_ontology.storage import digest


CONNECTION = {"base_url": "https://fixture.invalid/v1"}
PARAMETERS = {"model": "fixture", "temperature": 0, "max_tokens": 1000}


def node(ident):
    return {"id": ident, "name": "Concept", "kind": "Disease", "aliases": [],
            "standard_ids": [], "definition": "", "parent_ids": [], "is_root": False}


class Provider:
    def __init__(self):
        self.calls = []

    def generate(self, stage, prompt, payload, parameters):
        self.calls.append((stage, prompt))
        return {"finish_reason": "stop", "content": json.dumps({"rows": [
            {"id": item["id"], "relation_type": "UNCERTAIN", "confidence": "low",
             "kg_entity_level": "unknown", "ontology_entity_level": "unknown",
             "same_scope": False, "rationale": "Insufficient evidence", "evidence": []}
            for item in payload["items"]]})}


class PromptSelectionTests(unittest.TestCase):
    def test_named_prompt_reaches_provider_and_ols_policy_is_unchanged(self):
        provider = Provider()
        with TemporaryDirectory() as tmp, \
                patch("entity_ontology.alignment.pipeline.read_settings", return_value=(CONNECTION, PARAMETERS)), \
                patch("entity_ontology.alignment.pipeline.live_provider", return_value=provider):
            model = AlignmentModel("unused", tmp, 3, prompt_name="sections")
            model.assess({"kg": node("kg"), "items": [node("o")]})
            self.assertEqual(provider.calls, [("alignment", PROMPT_VARIANTS["sections"])])
            self.assertEqual(model.policy, PROMPT_VARIANTS["sections"])
            self.assertEqual(model.settings["alignment_prompt"], "sections")
            self.assertEqual(model.settings["alignment_prompt_sha256"], digest(model.policy))
            self.assertEqual(model.journal.prompts["ols_reference_action"], OLS_POLICY)
            self.assertEqual(model.journal.settings["alignment"], PARAMETERS)

    def test_default_uses_registry_default_and_legacy_stays_frozen(self):
        with TemporaryDirectory() as tmp, \
                patch("entity_ontology.alignment.pipeline.read_settings", return_value=(CONNECTION, PARAMETERS)), \
                patch("entity_ontology.alignment.pipeline.live_provider", return_value=Provider()):
            model = AlignmentModel("unused", tmp, 3)
            self.assertEqual(model.prompt_name, DEFAULT_PROMPT)
            self.assertEqual(model.policy, PROMPT_VARIANTS[DEFAULT_PROMPT])
            self.assertEqual(PROMPT_VARIANTS["legacy"], POLICY)

    def test_changed_prompt_cannot_reuse_output_even_with_same_model_settings(self):
        provider = Provider()
        with TemporaryDirectory() as tmp, \
                patch("entity_ontology.alignment.pipeline.read_settings", return_value=(CONNECTION, PARAMETERS)), \
                patch("entity_ontology.alignment.pipeline.live_provider", return_value=provider):
            graph, kg = {"nodes": [node("o")]}, {"nodes": [node("kg")]}
            first = AlignmentModel("unused", tmp, 5, prompt_name="legacy")
            run_alignment(graph, kg, tmp, model=first, use_ols=False)
            before = (Path(tmp) / "manifest.json").read_bytes()
            second = AlignmentModel("unused", tmp, 5, prompt_name="schema")
            with self.assertRaisesRegex(ValueError, "different inputs/settings"):
                run_alignment(graph, kg, tmp, model=second, use_ols=False)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual((Path(tmp) / "manifest.json").read_bytes(), before)
            settings = json.loads(before)["settings"]
            self.assertEqual(settings["alignment_prompt"], "legacy")
            self.assertEqual(settings["prompt_sha256"], digest(PROMPT_VARIANTS["legacy"]))

    def test_unknown_prompt_fails_before_configuration_or_provider(self):
        with patch("entity_ontology.alignment.pipeline.read_settings") as read, \
                patch("entity_ontology.alignment.pipeline.live_provider") as live:
            with self.assertRaisesRegex(ValueError, "Unknown alignment prompt"):
                AlignmentModel("unused", "/unused", 3, prompt_name="missing")
            read.assert_not_called()
            live.assert_not_called()

    def test_fake_models_record_legacy_policy_without_new_attributes(self):
        class Fake:
            settings = {}
            journal = SimpleNamespace(last_ref=None)

            def assess(self, payload):
                return []

        with TemporaryDirectory() as tmp:
            run_alignment({"nodes": []}, {"nodes": []}, tmp, model=Fake(), use_ols=False)
            settings = json.loads((Path(tmp) / "manifest.json").read_text())["settings"]
            self.assertEqual(settings["alignment_prompt"], "legacy")
            self.assertEqual(settings["prompt_sha256"], digest(POLICY))

    def test_cli_accepts_registry_choice_and_rejects_unknown_before_io(self):
        arguments = ["run", "--ontology", "source.json", "--kg-snapshot", "kg.json", "--output", "out"]
        with patch("entity_ontology.alignment.__main__.load_ontology") as load, redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as error:
                main([*arguments, "--alignment-prompt", "missing"])
            self.assertEqual(error.exception.code, 2)
            load.assert_not_called()
        with TemporaryDirectory() as tmp, \
                patch("entity_ontology.alignment.__main__.load_ontology", return_value={"nodes": []}), \
                patch("entity_ontology.alignment.__main__.load_kg_snapshot", return_value={"nodes": []}), \
                patch("entity_ontology.alignment.__main__.AlignmentModel") as model, \
                patch("entity_ontology.alignment.__main__.run_alignment", return_value={}):
            result = main(["run", "--ontology", "source.json", "--kg-snapshot", "kg.json", "--output", tmp,
                           "--alignment-prompt", "sections", "--no-ols"])
            self.assertEqual(result, 0)
            self.assertEqual(model.call_args.kwargs["prompt_name"], "sections")


if __name__ == "__main__":
    unittest.main()
