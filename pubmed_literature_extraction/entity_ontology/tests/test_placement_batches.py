"""Test bounded placement proposals and independent review without a provider."""

from collections import Counter
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
import unittest
from unittest.mock import patch

from entity_ontology.hierarchy import placement
from entity_ontology.hierarchy.entity_placement_dag import EntityDAG
from entity_ontology.hierarchy.order_entities import order_input_sha256
from entity_ontology.hierarchy.placement import (
    GO_REVIEW, NEW_PARENT_PAIR, NEW_ROOT_PAIR, PLACEMENT_BATCH_SIZE, PROPOSE, REFERENCE_QUESTION_LIMIT,
    REVIEW, SHARDED_PROPOSE, describe, place_one, review_item,
)
from entity_ontology.model import ProviderError
from entity_ontology.storage import atomic_json, read_json


def concept(index, kind="Protein"):
    return {
        "id": f"concept-{index}", "name": f"Class {index}", "kind": kind,
        "definition": f"Synthetic class {index}.", "aliases": [],
        "source_mentions": [f"source-{index}"], "origin": "literature_mention",
    }


def seeded_dag(count, kind="Protein"):
    dag = EntityDAG([kind])
    candidates = [concept(i, kind) for i in range(count)]
    for candidate in candidates:
        dag.place(candidate)
    return dag, candidates


def proposal(item, *, narrower=False, suggestion=None):
    return [{
        "id": item["id"], "new_parent": suggestion,
        "comparisons": [
            {"id": candidate["id"], "a_in_b": narrower, "b_in_a": False, "reason": "fixture"}
            for candidate in item["candidates"]
        ],
    }]


def reviews(items, verdict):
    return [{"id": item["id"], "supported": verdict, "reason": "fixture"} for item in items]


class PlacementBatchTest(unittest.TestCase):
    def test_sourced_proposal_retries_only_omitted_candidates(self):
        dag, candidates = seeded_dag(4)
        target = {**concept("target"), "origin": "kg_enrichment",
                  "definition_evidence": {"database": "Fixture"}}
        calls = []
        omitted = candidates[1]["id"]

        def request(task):
            stage, _, payload = task
            if stage == "incremental_edge_review":
                return reviews(payload["items"], True)
            calls.append(payload)
            response = proposal(payload["items"][0], narrower=len(calls) == 2)
            if len(calls) == 1:
                response[0]["comparisons"].pop(1)
            return response

        result = place_one(dag, target, request=request)
        self.assertEqual(len(calls), 2)
        self.assertEqual([row["id"] for row in calls[1]["items"][0]["candidates"]], [omitted])
        self.assertEqual(calls[0]["current_edges"], calls[1]["current_edges"])
        self.assertEqual(calls[0]["type_root"], calls[1]["type_root"])
        self.assertEqual(result["parent_ids"], [omitted])
        self.assertEqual([row["candidate_id"] for row in result["comparisons"]],
                         [row["id"] for row in candidates])

    def test_invalid_partial_proposals_retry_every_unverified_id(self):
        candidates = {node["id"]: describe(node) for node in [concept(i) for i in range(3)]}
        target = describe(concept("target"))
        cases = {
            "duplicate": {"id": "concept-1"},
            "unknown_id": {"id": "unknown"},
            "invalid_response_value": {"a_in_b": "false"},
        }
        for name, corruption in cases.items():
            with self.subTest(name=name):
                calls = []

                def request(task):
                    item = task[2]["items"][0]
                    calls.append([row["id"] for row in item["candidates"]])
                    response = proposal(item)
                    if len(calls) == 1:
                        response[0]["comparisons"].pop()
                        response[0]["comparisons"][0].update(corruption)
                    return response

                result, _ = placement.propose_placement(request, target, candidates, {}, repair_missing=True)
                missing = list(candidates) if name == "duplicate" else ["concept-0", "concept-2"]
                self.assertEqual(calls, [list(candidates), missing])
                self.assertEqual(set(result), candidates.keys())

    def test_foreign_id_substitution_preserves_valid_rows_and_never_guesses_a_verdict(self):
        target = describe(concept("target"))
        candidates = {row["id"]: row for row in (describe(concept(i)) for i in range(8))}
        calls = []

        def request(task):
            payload = task[2]
            item = payload["items"][0]
            calls.append([row["id"] for row in item["candidates"]])
            rows = proposal(item)
            if len(calls) == 1:
                rows[0]["comparisons"][3].update(id="unrequested", a_in_b=True)
            else:
                self.assertEqual(payload["required_candidate_ids"], ["concept-3"])
            return rows

        complete, _ = placement.propose_placement(request, target, candidates, {}, repair_missing=True)
        self.assertEqual(calls, [list(candidates), ["concept-3"]])
        self.assertEqual(set(complete), set(candidates))
        self.assertFalse(complete["concept-3"]["a_in_b"])

    def test_omitted_repair_must_return_exact_ids_without_a_new_parent(self):
        targets = {node["id"]: describe(node) for node in [concept(0), concept(1)]}
        target = describe(concept("source"))
        for mutation in ({"comparisons": []}, {"new_parent": {"name": "new", "definition": "new", "reason": "new"}}):
            with self.subTest(mutation=mutation):
                calls = []

                def request(task):
                    item = task[2]["items"][0]
                    calls.append(item)
                    rows = proposal(item)
                    if len(calls) == 1:
                        rows[0]["comparisons"].pop()
                    else:
                        rows[0].update(mutation)
                    return rows

                with self.assertRaises(placement.InvalidJudgment):
                    placement.propose_placement(request, target, targets, {}, repair_missing=True)
                self.assertEqual(len(calls), 2)

    def test_literature_target_continues_to_retry_the_whole_proposal(self):
        dag, _ = seeded_dag(3)
        target = {**concept("literature"), "definition_evidence": {"database": "Fixture"}}
        counts = []

        def request(task):
            item = task[2]["items"][0]
            counts.append(len(item["candidates"]))
            rows = proposal(item)
            if len(counts) == 1:
                rows[0]["comparisons"].pop()
            return rows

        place_one(dag, target, request=request)
        self.assertEqual(counts, [3, 3])

    def test_boundary_keeps_original_prompts_and_payloads(self):
        count = PLACEMENT_BATCH_SIZE
        dag, candidates = seeded_dag(count)
        target = concept("target")
        calls = []

        def request(task):
            calls.append(task)
            stage, _, payload = task
            if stage == "incremental_propose":
                return proposal(payload["items"][0], narrower=True)
            return reviews(payload["items"], True)

        context = {
            "type_root": describe(dag.nodes["type:Protein"]),
            "current_edges": [{"child_id": child, "parent_id": parent} for child, parent in sorted(dag.edges)],
        }
        result = place_one(dag, target, request=request)
        self.assertEqual(calls, [
            ("incremental_propose", PROPOSE, {
                "items": [{**describe(target), "candidates": [describe(candidate) for candidate in candidates]}],
                **context,
            }),
            ("incremental_edge_review", REVIEW, {
                "items": [review_item(f"existing:{candidate['id']}:parent", describe(target), describe(candidate))
                          for candidate in candidates],
                **context,
            }),
        ])
        self.assertEqual(len(result["parent_ids"]), count)

    def test_shards_keep_every_candidate_review_and_go_decision(self):
        count = PLACEMENT_BATCH_SIZE * 2 + 6
        dag, candidates = seeded_dag(count, "Pathway")
        target = concept("target", "Pathway")
        calls, lookups = [], []

        def request(task):
            stage, prompt, payload = task
            items = payload["items"]
            if stage == "incremental_propose":
                self.assertEqual(prompt, SHARDED_PROPOSE)
                calls.append((stage, len(items[0]["candidates"])))
                return proposal(items[0], narrower=True)
            self.assertEqual(prompt, GO_REVIEW if stage == "incremental_go_review" else REVIEW)
            calls.append((stage, len(items)))
            return reviews(items, True if stage == "incremental_go_review" else None)

        def lookup(item, pairs):
            lookups.append((item, pairs))
            details = [
                {"go_id": term_id, "name": local["name"], "definition": local["definition"], "source": "synthetic"}
                for term_id, local in [("GO:0000000", item), ("GO:0000001", candidates[0])]
            ]
            return {
                "concept_id": item["id"], "status": "MATCHED", "go_id": "GO:0000000",
                "go_name": "Fixture", "go_definition": "A synthetic fixture.",
                "relation_type": "fixture", "source": "synthetic", "ontology_relations": [
                    {"subject": "GO:0000000", "object": "GO:0000001", "predicate": "is_a", "source": "synthetic"},
                ],
                "query_records": [{"tool": "detail", "ok": True, "result": {"terms": [row]}} for row in details],
                "term_query_id": "test",
            }

        result = place_one(dag, target, request=request, go_lookup=lookup)
        for stage in ("incremental_propose", "incremental_edge_review"):
            self.assertEqual(Counter(size for name, size in calls if name == stage),
                             Counter({PLACEMENT_BATCH_SIZE: 2, 6: 1}))
        self.assertEqual([size for name, size in calls if name == "incremental_go_review"], [1])
        self.assertEqual(len(lookups), 1)
        self.assertEqual(len(lookups[0][1]), REFERENCE_QUESTION_LIMIT)
        self.assertEqual([row["candidate_id"] for row in result["comparisons"]],
                         [row["id"] for row in candidates])
        self.assertTrue(all(row["reviews"][0]["supported"] is None for row in result["comparisons"]))
        self.assertTrue(result["comparisons"][0]["go_reviews"][0]["supported"])
        self.assertTrue(all(not row["go_reviews"] for row in result["comparisons"][1:]))
        self.assertEqual(result["parent_ids"], [candidates[0]["id"]])
        self.assertEqual(len(result["go_supported_edges"]), 1)

    def test_shards_choose_one_valid_intermediate_parent_in_order(self):
        dag, candidates = seeded_dag(PLACEMENT_BATCH_SIZE * 2 + 1)
        target = concept("target")
        reviewed = []

        def request(task):
            stage, _, payload = task
            items = payload["items"]
            if stage == "incremental_propose":
                first_id = items[0]["candidates"][0]["id"]
                name = {
                    "concept-0": candidates[-1]["name"],
                    f"concept-{PLACEMENT_BATCH_SIZE}": "First novel class",
                    f"concept-{PLACEMENT_BATCH_SIZE * 2}": "Second novel class",
                }[first_id]
                return proposal(items[0], suggestion={"name": name, "definition": "Fixture parent", "reason": "fixture"})
            reviewed.extend(item["id"] for item in items)
            return reviews(items, True)

        result = place_one(dag, target, request=request)
        self.assertEqual(reviewed, [NEW_PARENT_PAIR, NEW_ROOT_PAIR])
        self.assertEqual(dag.nodes[result["generated_parent_id"]]["name"], "First novel class")
        self.assertEqual(result["parent_ids"], [result["generated_parent_id"]])

    def test_parallel_types_produce_identical_ranked_artifacts_and_requests(self):
        kinds = ["Protein", "Disease", "Pathway"]
        concepts = [concept(f"{kind}-{i}", kind)
                    for kind in kinds for i in range(PLACEMENT_BATCH_SIZE + 2)]
        expected_ids = [row["id"] for row in concepts]
        order = {
            "status": "complete", "input_sha256": order_input_sha256(concepts),
            "groups": [{"kind": kind, "concept_ids": [row["id"] for row in concepts if row["kind"] == kind]}
                       for kind in kinds],
        }
        starting = Barrier(len(kinds))
        lock = Lock()
        active = peak = 0
        mode = "serial"
        logs = {"serial": [], "parallel": []}
        snapshots = []

        def request(_session, task):
            nonlocal active, peak
            stage, _, payload = task
            items = payload["items"]
            with lock:
                logs[mode].append(json.dumps(task, sort_keys=True))
                active += 1
                peak = max(peak, active)
            try:
                if stage == "incremental_propose":
                    item = items[0]
                    if mode == "parallel" and item["id"].endswith("-0"):
                        starting.wait(timeout=5)
                    reason = {"name": "Intermediate class", "definition": "Fixture", "reason": "fixture"}
                    result = proposal(item, suggestion=reason if item["id"].endswith("-0") else None)
                    for row in result[0]["comparisons"]:
                        row["a_in_b"] = row["id"].endswith("-0")
                    return result
                return reviews(items, True)
            finally:
                with lock:
                    active -= 1

        def record_write(path, data):
            if Path(path).name == "progress.json" and mode == "parallel":
                snapshots.append((data["processed"], [row["concept_id"] for row in data["decisions"]]))
            atomic_json(path, data)

        with TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            root = Path(directory)
            atomic_json(root / "equivalence.json", {"concepts": concepts})
            atomic_json(root / "concept_order.json", order)
            with (
                patch.object(placement.ModelSession, "req", request),
                patch.object(placement.ModelSession, "request_settings", return_value=({}, {"model": "fixture"})),
                patch.object(placement, "atomic_json", record_write),
            ):
                fixture_config = root / "config.json"
                placement.run(root, root / "serial", config=fixture_config, go_reference=False)
                mode = "parallel"
                with patch.object(placement, "PARALLEL_KIND_MIN_CONCEPTS", 1):
                    placement.run(root, root / "parallel", config=fixture_config, go_reference=False)
            for name in ("placement.json", "progress.json"):
                self.assertEqual((root / "serial" / name).read_bytes(), (root / "parallel" / name).read_bytes())
            self.assertEqual(read_json(root / "parallel" / "progress.json")["processed"], len(concepts))

        self.assertEqual(Counter(logs["serial"]), Counter(logs["parallel"]))
        self.assertGreaterEqual(peak, len(kinds))
        self.assertLessEqual(peak, placement.MAX_CONCURRENT_REQUESTS)
        self.assertEqual(snapshots[0], (0, []))
        self.assertEqual(snapshots[-1], (len(concepts), expected_ids))
        for count, ids in snapshots:
            self.assertEqual(count, len(ids))
            included = set(ids)
            self.assertEqual(ids, [ident for ident in expected_ids if ident in included])

    def test_parallel_failure_stops_queued_types_and_flushes_progress(self):
        concepts = [concept("first"), concept("failure"), concept("queued", "Disease")]
        calls, snapshots = [], []

        def request(task):
            item = task[2]["items"][0]
            calls.append(item["id"])
            if item["id"] == concepts[1]["id"]:
                raise ProviderError("fixture provider stopped")
            return proposal(item)

        with redirect_stdout(StringIO()), patch.object(placement, "KIND_WORKERS", 1):
            with self.assertRaisesRegex(ProviderError, "fixture provider stopped"):
                placement.place_by_kind(
                    concepts, ["Protein", "Disease"], request=request,
                    go_lookup=None, save_progress=snapshots.append,
                )
        self.assertEqual(calls, [row["id"] for row in concepts[:2]])
        self.assertEqual([[row["concept_id"] for row in snapshot] for snapshot in snapshots],
                         [[], [concepts[0]["id"]]])
