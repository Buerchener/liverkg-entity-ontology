"""Test equivalent workload, per-request pricing and local-cache accounting."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from entity_ontology.experiments.enrichment_v3 import _alias_dispositions, _alias_transitions, _usage, journals
from entity_ontology.storage import atomic_json, digest


def response(tokens=100):
    return {"usage": {"prompt_tokens": tokens, "prompt_tokens_details": {"cached_tokens": 20},
                      "completion_tokens": 10, "total_tokens": tokens + 10}}


class PairedExperimentTests(TestCase):
    def test_exact_shared_name_is_resolved_only_for_same_previously_observed_candidate(self):
        own = "KG-" + digest(["db", "element"])[:20]
        matches = [{"ontology_node_id": ident, "ontology_name": "Shared", "shared_names": ["shared"],
                    "same_type": True} for ident in (own, "class")]
        record = {"kg": {"source": {"database": "db", "element_id": "element"}}, "realignment": {
            "identity_evidence": {"exact_matches": matches, "unresolved_exact_matches": ["class"]}}}
        old = _alias_dispositions(record)
        evidence = record["realignment"]["identity_evidence"]
        evidence.update(unresolved_exact_matches=[], scope_resolutions=[{
            "ontology_node_id": "class", "relation": "IS_A", "basis": "authority_asserted_strict_scope"}])
        new = _alias_dispositions(record)
        row, = _alias_transitions({"alias_dispositions": old}, {"alias_dispositions": new})
        self.assertEqual((row["before"], row["after"]), ("unresolved", "resolved_distinct_by_authority"))
        self.assertEqual(len(new), 1)

    def test_prices_prompt_tier_and_counts_provider_cached_tokens_once(self):
        cheap = _usage(response(), "qwen3.7-flash")
        self.assertEqual(cheap["total_tokens"], 110)
        self.assertAlmostEqual(cheap["list_cny_max"], (80 * 0.2 + 20 * 0.04 + 10 * 0.8) / 1e6)
        long = _usage(response(40000), "qwen3.7-flash")
        self.assertAlmostEqual(long["list_cny_min"], (39980 * 0.6 + 20 * 0.12 + 10 * 2.4) / 1e6)
        reported = response()
        del reported["usage"]["prompt_tokens_details"]
        unknown_cache = _usage(reported, "qwen3.7-plus")
        self.assertLess(unknown_cache["list_cny_min"], unknown_cache["list_cny_max"])
        self.assertEqual(_usage({}, "qwen3.7-plus")["usage_unavailable"], 1)

    def test_local_reuses_unused_preloaded_answers_and_failed_fresh_requests(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            entries = [{"file": f"llm/{i}.json", "request_sha256": str(i)} for i in range(4)]
            for i, ref in enumerate(entries):
                item = {"request_sha256": str(i), "request": {"stage": "alignment"},
                        "status": "provider_error" if i == 3 else "response_received"}
                if i != 3:
                    item["response"] = response()
                atomic_json(folder / ref["file"], item)
            atomic_json(folder / "cache-origin.json", {"model_requests": entries[:2]})
            align = {"alignment": {"evidence": {"model_call_refs": [entries[2]["file"]]}}}
            rows = [{"kg": {"id": "x"}, "model_call_refs": [entries[0]["file"], entries[0]["file"]],
                     "verification": align, "realignment": align}]
            usage = journals(folder, rows, "qwen3.7-plus")
            self.assertEqual(usage["unused_imported_responses"], 1)
            self.assertEqual(usage["logical_work"]["journal_requests"], 3)
            self.assertEqual(usage["logical_work"]["total_tokens"], 220)
            self.assertEqual(usage["fresh_provider_attempts"]["total_tokens"], 110)
            self.assertEqual(usage["fresh_provider_attempts"]["usage_unavailable"], 1)
            self.assertEqual(usage["per_node"]["x"]["journal_requests"], 2)
