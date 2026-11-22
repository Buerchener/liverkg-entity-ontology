import json
from pathlib import Path
import tempfile
import threading
import unittest

from entity_ontology.alignment.prompt_evaluation import run_evaluation
from entity_ontology.model import ProviderError


PARAMETERS = {"model": "fixture", "temperature": 0, "max_tokens": 1000}
CONNECTION = {"kind": "openai_compatible", "base_url": "https://fixture.invalid/v1",
              "api_key_env": "UNUSED_FIXTURE_KEY", "timeout_seconds": 1}
PROMPTS = {"legacy": "legacy", "sections": "sections", "schema": "schema"}


def corpus():
    return {"cases": [{"case_id": "case-a", "payload": {
        "kg": {"id": "kg", "name": "test", "kind": "Gene", "definition": "", "aliases": [],
               "standard_ids": [], "entity_level": "unknown", "taxon_id": "9606"},
        "items": [{"id": "o", "name": "test", "kind": "Gene", "definition": "", "aliases": [],
                   "standard_ids": [], "entity_level": "class"}],
    }}]}


def row(**extra):
    return {"id": "o", "relation_type": "UNCERTAIN", "confidence": "low",
            "kg_entity_level": "unknown", "ontology_entity_level": "class",
            "same_scope": False, "rationale": "Insufficient supplied evidence", "evidence": [], **extra}


class Provider:
    def __init__(self, handler):
        self.handler, self.calls, self.lock = handler, [], threading.Lock()

    def generate(self, stage, prompt, payload, parameters):
        with self.lock:
            self.calls.append((stage, prompt, payload, dict(parameters)))
        result = self.handler(prompt, payload)
        if isinstance(result, Exception):
            raise result
        return {"content": result if isinstance(result, str) else json.dumps(result),
                "model": parameters["model"], "finish_reason": "stop",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class PromptEvaluationTests(unittest.TestCase):
    def run_fixture(self, directory, provider, **kwargs):
        return run_evaluation(kwargs.pop("data", corpus()), directory,
                              provider=provider, connection=CONNECTION, parameters=PARAMETERS,
                              prompt_variants=kwargs.pop("variants", PROMPTS), **kwargs)

    def test_identical_inputs_common_repair_raw_journal_and_zero_charge_resume(self):
        def handler(prompt, payload):
            if prompt == "legacy" and "output_format_correction" not in payload:
                return {"rows": [row(evidence=[{"side": "kg", "field": "name", "quote": "fabricated"}])]}
            return {"rows": [row()]}
        provider = Provider(handler)
        with tempfile.TemporaryDirectory() as tmp:
            summary = self.run_fixture(tmp, provider, max_calls=6)
            self.assertEqual(len(provider.calls), 4)
            self.assertEqual(summary["by_prompt"]["legacy"]["first_pass_rate"], 0)
            self.assertEqual(summary["by_prompt"]["legacy"]["post_retry_rate"], 1)
            self.assertEqual(summary["by_prompt"]["legacy"]["tokens"]["total_tokens"], 30)
            first = [c for c in provider.calls if "output_format_correction" not in c[2]]
            self.assertEqual(len(first), 3)
            self.assertTrue(all(c[2] == corpus()["cases"][0]["payload"] for c in first))
            self.assertTrue(all(c[3] == PARAMETERS for c in provider.calls))
            records = [json.loads(p.read_text()) for p in (Path(tmp) / "calls").glob("*.json")]
            self.assertTrue(all("body" in r["request"] and "response" in r and "latency_seconds" in r for r in records))
            trials = [json.loads(p.read_text()) for p in (Path(tmp) / "results").glob("*.json")]
            failed = next(t for t in trials if t["prompt_variant"] == "legacy")
            self.assertEqual(failed["attempts"][0]["rows"][0]["evidence"][0]["quote"], "fabricated")
            self.assertIn("LOCAL", failed["attempts"][0]["validation_errors"][0])
            resumed = self.run_fixture(tmp, provider, max_calls=6)
            self.assertEqual(len(provider.calls), 4)
            self.assertEqual(resumed["new_provider_calls_this_invocation"], 0)
            self.assertEqual(resumed["cached_calls_this_invocation"], 4)

    def test_budget_covers_all_first_attempts_before_any_retry(self):
        provider = Provider(lambda prompt, payload: {"rows": []})
        with tempfile.TemporaryDirectory() as tmp:
            summary = self.run_fixture(tmp, provider, max_calls=3, workers=4)
            self.assertEqual(len(provider.calls), 3)
            self.assertTrue(all("output_format_correction" not in c[2] for c in provider.calls))
            self.assertFalse(summary["all_scheduled_attempts_executed"])
            for variant in PROMPTS:
                self.assertEqual(summary["by_prompt"][variant]["calls"], 1)
                self.assertEqual(summary["by_prompt"][variant]["budget_skipped_attempts"], 1)
            self.run_fixture(tmp, provider, max_calls=3, workers=4)
            self.assertEqual(len(provider.calls), 3)

    def test_invalid_citations_do_not_hide_raw_semantic_risk(self):
        proposed = row(relation_type="MAPS_TO", confidence="high", same_scope=True,
                       evidence=[{"side": "kg", "field": "name", "quote": "not in local source"}])
        provider = Provider(lambda prompt, payload: {"rows": [proposed]})
        with tempfile.TemporaryDirectory() as tmp:
            summary = self.run_fixture(tmp, provider, prompts=["legacy"], max_calls=2)
            for phase in ("first_pass", "final"):
                metrics = summary["by_prompt"]["legacy"][phase]
                self.assertEqual(metrics["raw_relation_counts"]["MAPS_TO"], 1)
                self.assertEqual(metrics["rule_conflict_counts"]["maps_to_species_scope_unknown_on_one_side"], 1)
                self.assertEqual(metrics["positive_without_verified_two_sided_local_citations"], 1)
                self.assertEqual(metrics["admitted_pair_proposal_counts"], {})

    def test_unsupported_instance_and_empty_evidence_are_counted(self):
        provider = Provider(lambda p, x: {"rows": [row(relation_type="INSTANCE_OF", confidence="high",
                                                        kg_entity_level="individual", same_scope=True)]})
        with tempfile.TemporaryDirectory() as tmp:
            summary = self.run_fixture(tmp, provider, prompts=["legacy"], max_calls=2)
            metrics = summary["by_prompt"]["legacy"]["final"]
            self.assertEqual(metrics["positive_empty_evidence"], 1)
            self.assertEqual(metrics["rule_conflict_counts"]["instance_of_individual_membership_not_established"], 1)
            self.assertEqual(metrics["admitted_pair_proposal_counts"], {})

    def test_frozen_input_prompt_and_settings_changes_refused_before_call(self):
        provider = Provider(lambda p, x: {"rows": [row()]})
        with tempfile.TemporaryDirectory() as tmp:
            self.run_fixture(tmp, provider, max_calls=6)
            changed = corpus()
            changed["cases"][0]["payload"]["kg"]["name"] = "changed"
            with self.assertRaisesRegex(ValueError, "Frozen experiment"):
                self.run_fixture(tmp, provider, data=changed, max_calls=6)
            with self.assertRaisesRegex(ValueError, "Frozen experiment"):
                self.run_fixture(tmp, provider, variants={**PROMPTS, "legacy": "changed"}, max_calls=6)
            with self.assertRaisesRegex(ValueError, "Frozen experiment"):
                self.run_fixture(tmp, provider, max_calls=7)
            self.assertEqual(len(provider.calls), 3)

    def test_repeats_are_independent_paid_trials_not_deduplicated(self):
        provider = Provider(lambda p, x: {"rows": [row()]})
        with tempfile.TemporaryDirectory() as tmp:
            summary = self.run_fixture(tmp, provider, max_calls=12, repeats=2)
            self.assertEqual(summary["trial_count"], 6)
            self.assertEqual(len(provider.calls), 6)
            self.run_fixture(tmp, provider, max_calls=12, repeats=2)
            self.assertEqual(len(provider.calls), 6)

    def test_provider_error_does_not_masquerade_as_contract_retry(self):
        provider = Provider(lambda p, x: ProviderError("fixture unavailable"))
        with tempfile.TemporaryDirectory() as tmp:
            summary = self.run_fixture(tmp, provider, max_calls=6)
            self.assertEqual(len(provider.calls), 3)
            self.assertTrue(all(v["provider_error_calls"] == 1 for v in summary["by_prompt"].values()))
            self.run_fixture(tmp, provider, max_calls=6)
            self.assertEqual(len(provider.calls), 3)

    def test_malformed_relation_is_preserved_without_crashing_audit(self):
        provider = Provider(lambda p, x: {"rows": [row(relation_type=[])]})
        with tempfile.TemporaryDirectory() as tmp:
            summary = self.run_fixture(tmp, provider, prompts=["legacy"], max_calls=2)
            self.assertEqual(summary["by_prompt"]["legacy"]["final"]["raw_relation_counts"], {"INVALID": 1})
            self.assertEqual(summary["by_prompt"]["legacy"]["post_retry_valid"], 0)

    def test_rejects_underfunded_first_pass_and_excessive_workers(self):
        provider = Provider(lambda p, x: {"rows": [row()]})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "first attempt"):
                self.run_fixture(tmp, provider, max_calls=2)
            with self.assertRaisesRegex(ValueError, "workers"):
                self.run_fixture(tmp, provider, workers=5)
            self.assertFalse(provider.calls)


if __name__ == "__main__":
    unittest.main()
