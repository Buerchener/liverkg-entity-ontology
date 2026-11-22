import json
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from entity_ontology.experiments.enrichment_v4 import keys, stratified, warm_usage
from entity_ontology.storage import digest


def example(kind, ident, aliases=()):
    return {
        "id": ident,
        "kind": kind,
        "name": "name_" + ident,
        "aliases": list(aliases),
        "standard_ids": ["id_" + ident],
    }


class EnrichmentCohortTests(TestCase):
    def test_seeded_round_robin_preserves_scarce_entities_for_separate_scale_run(self):
        pool = [
            example("Tissue", "t1"),
            example("Tissue", "t2"),
            *(example("Gene", f"g{i}") for i in range(9)),
        ]
        chosen, design = stratified(pool, 6, 725, reserve=1)
        self.assertEqual(design["sample_by_type"], {"Gene": 5, "Tissue": 1})
        self.assertEqual(chosen, stratified(reversed(pool), 6, 725, reserve=1)[0])
        available = [r for r in pool if r not in chosen]
        larger, _ = stratified(available, 4, 726, reserve=0)
        self.assertEqual(Counter(r["kind"] for r in larger), {"Gene": 3, "Tissue": 1})
        self.assertFalse({r["id"] for r in larger} & {r["id"] for r in chosen})

    def test_two_distinct_ids_cannot_reintroduce_same_type_prior_alias(self):
        initial = example("Gene", "first", ["prior alias"])
        duplicate = example("Gene", "second", ["Prior Alias"])
        cross_type = example("Protein", "other", ["Prior Alias"])
        self.assertTrue(keys(initial) & keys(duplicate))
        self.assertFalse(keys(initial) & keys(cross_type))
        selected, _ = stratified([initial, duplicate, cross_type], 2, 7, reserve=0)
        self.assertEqual(len(selected), 2)
        with self.assertRaisesRegex(ValueError, "distinct eligible"):
            stratified([initial, duplicate, cross_type], 3, 7, reserve=0)


class WarmRequestAccountingTests(TestCase):
    def test_imported_prior_error_is_counted_only_when_exact_context_was_used(self):
        with TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "llm").mkdir()
            original = {"stage": "alignment", "input": {"items": [{"id": "x"}]}}
            correction = {
                **original,
                "input": {
                    **original["input"],
                    "output_format_correction": "bad cite. Return every candidate once with citations.",
                },
            }
            other = {**original, "input": {"items": [{"id": "y"}]}}
            requests = []
            for seq, request in enumerate([original, correction, other]):
                saved = {
                    "request": request,
                    "request_sha256": digest(request),
                    "status": "response_received" if seq == 1 else "response_invalid",
                    "validation_error": "bad cite",
                    "response": {
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                            "prompt_tokens_details": {"cached_tokens": 0},
                        }
                    },
                }
                ref = f"llm/{seq}.json"
                (run / ref).write_text(json.dumps(saved))
                # The original invalid response and the unrelated invalid response
                # were received before the patch. The correction was sent live.
                if seq != 1:
                    requests.append({"file": ref, "request_sha256": digest(request)})
            (run / "cache-origin.json").write_text(
                json.dumps({"model_requests": requests})
            )
            usage = warm_usage(
                run,
                [{"kg": {"id": "x"}, "model_call_refs": ["llm/1.json"]}],
                "qwen3.7-flash",
            )
            self.assertEqual(usage["logical_work"]["total_tokens"], 30)
            self.assertEqual(usage["fresh_provider_attempts"]["total_tokens"], 15)
            self.assertEqual(usage["unused_imported_responses"], 1)
            self.assertEqual(
                usage["prior_invalid_alignment_responses_used_for_correction"],
                ["llm/0.json"],
            )
