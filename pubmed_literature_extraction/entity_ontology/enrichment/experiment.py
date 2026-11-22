"""Freeze the evidence experiment's cohorts and compare completed run artifacts."""

import argparse
from collections import Counter
import json
from pathlib import Path

from ..storage import atomic_json, digest, read_json, run_lock
from .inputs import _balanced_sample, _file_reference, load_enrichment_inputs
from .parent_fallback import accepted_relation


_PARENT_FAILURE = "The proposed new parent lacks a verified authoritative definition and strict IS_A edge."


def freeze(pilot, directory, *, seed=20261123):
    pilot, directory = Path(pilot).resolve(), Path(directory).resolve()
    manifest = read_json(pilot / "manifest.json")
    sample = read_json(pilot / "sample.json")["nodes"]
    records = read_json(pilot / "records.json")
    if (manifest["status"] != "complete"
            or len({n["id"] for n in sample}) != 42
            or [r["kg"]["id"] for r in records] != [n["id"] for n in sample]):
        raise ValueError("Use the complete frozen 42-record enrichment pilot")
    kg = manifest["inputs"]["kg_snapshot"]["snapshot_file"]
    alignments = manifest["inputs"]["alignment_files"]
    inputs = load_enrichment_inputs(kg["path"], [row["path"] for row in alignments])
    if (inputs["metadata"]["kg_snapshot"]["snapshot_file"] != kg
            or inputs["metadata"]["alignment_files"] != alignments
            or _file_reference(Path(manifest["ontology"]["path"])) != manifest["ontology"]):
        raise ValueError("The pilot's frozen inputs changed")
    replay = [row["kg"] for row in records if row["status"] == "needs_review"
              and row["reason"] == _PARENT_FAILURE]
    if len(replay) != 12:
        raise ValueError("Expected 12 recorded parent-evidence failures in the verified pilot")
    excluded = set(inputs["baseline"]["covered_ids"]) | {node["id"] for node in sample}
    unseen, design = _balanced_sample(inputs["nodes"], excluded, 12, seed)
    common = {
        "version": "enrichment-frozen-cohort-v1",
        "kg_snapshot": kg, "alignment_files": alignments, "baseline_ontology": manifest["ontology"],
        "pilot_sample": _file_reference(pilot / "sample.json"),
        "baseline_pilot_manifest": _file_reference(pilot / "manifest.json"),
        "baseline_pilot_records": _file_reference(pilot / "records.json"),
    }
    selections = {
        "replay": (replay, {
            "status": "needs_review", "reason": _PARENT_FAILURE, "size": 12,
            "method": "all exact recorded parent evidence failures, ordered by frozen pilot records",
        }),
        "unseen": (unseen, {
            **design, "excluded_pilot_records": 42,
            "method": "deterministic stratification based solely on entity type, raw ID, prior supported coverage and pilot ID exclusions",
        }),
    }
    directory.mkdir(parents=True, exist_ok=True)
    frozen = {}
    with run_lock(directory):
        for cohort, (nodes, selection) in selections.items():
            path = directory / (cohort + "-12.json")
            doc = {**common, "cohort": cohort, "selection": selection,
                   "nodes": [{"id": n["id"], "kind": n["kind"]} for n in nodes]}
            frozen[cohort] = _write_frozen(path, {**doc, "signature": digest(doc)})
    return frozen


def _write_frozen(path, doc):
    if path.exists():
        if read_json(path) != doc:
            raise ValueError(f"An existing frozen artifact has different contents: {path}")
    else:
        atomic_json(path, doc)
    return _file_reference(path)


def _schema_counts(edges, inserted_ids):
    schema = [edge for edge in edges if edge["parent_id"].startswith("type:")]
    biomedical = [edge for edge in edges if not edge["parent_id"].startswith("type:")]
    root_of_kg = sum(edge["child_id"] in inserted_ids for edge in schema)
    parents_of_kg = [edge for edge in biomedical if edge["child_id"] in inserted_ids]
    return {
        "kg_to_schema_root": root_of_kg,
        "intermediate_to_schema_root": len(schema) - root_of_kg,
        "strict_biomedical": len(biomedical),
        "kg_to_biomedical_parent": len(parents_of_kg),
        "kg_nodes_with_published_biomedical_parent": len({edge["child_id"] for edge in parents_of_kg}),
        "other_biomedical_edges": len(biomedical) - len(parents_of_kg),
        "by_support": dict(Counter(edge["support"] for edge in edges)),
    }


def _confirmed_parents(placement):
    target = placement.get("proposal", {}).get("concept_id")
    reviewed = set()
    for edge in placement.get("edge_evidence", []):
        if edge["child_id"] != target or edge["parent_id"].startswith("type:"):
            continue
        if edge.get("support") == "external_parent_fallback":
            continue  # Use the independently reviewed fallback candidate below.
        typed = edge.get("typed_relation_review")
        if typed is not None and not accepted_relation(typed):
            continue
        local = (edge.get("review") or {}).get("supported")
        external = (edge.get("reference_review") or {}).get("supported")
        if local is False or (local is not True and external is not True):
            continue
        parent = ((edge.get("reference") or {}).get("parent_term") or {}).get("term_id")
        reviewed.add(parent or edge["parent_id"])
    fallback = placement.get("parent_fallback") or {}
    blocked = set()
    for candidate in fallback.get("candidates", []):
        if candidate["status"] != "verified":
            continue
        ident = candidate["term"]["term_id"]
        if (fallback["status"] in {"structural_rejection", "ancestor_conflict", "import_scope_unverified"}
                and candidate.get("id") == fallback.get("selected_parent_id")):
            blocked.add(ident)
        else:
            reviewed.add(ident)
    return reviewed - blocked, blocked


def _rate(numerator, denominator):
    return numerator / denominator if denominator else None


def _nonempty_result(query):
    result = query.get("result") or {}
    return any(result.get(field) for field in ("record", "terms", "hits"))


def _lookup_results(records, attempts):
    counts = Counter()
    for query in records:
        if _nonempty_result(query):
            counts["returned"] += 1
        elif query.get("status") == "lookup_failed" or (query.get("result") or {}).get("reason") == "LOOKUP_FAILED":
            counts["failed"] += 1
        else:
            counts["empty"] += 1
    return {
        "attempts": attempts, "nonempty_returns": counts["returned"],
        "nonempty_return_rate": _rate(counts["returned"], attempts),
        "lookup_failures": counts["failed"], "empty_results": counts["empty"],
    }


def _ols_metrics(records, audits):
    queries = [query for audit in audits for query in audit["result"].get("query_records", [])]
    executed = [query for query in queries if query.get("status") in {"observed", "lookup_failed"}]
    invalid = [query for query in queries if query.get("status") == "invalid_action"]
    counts = Counter(query.get("status") for query in queries)
    attempts = sum(audit["result"].get("tool_calls", 0) for audit in audits)
    observations = [o for row in records for o in row.get("source_audit", {}).get("observations", [])]
    inspections = [o for o in observations if o.get("action") == "inspect" and o.get("source")]
    definition_queries = [o for o in observations if o.get("source") in {"MONDO", "CL", "Uberon", "ChEBI"}
                          and not o.get("action") and o.get("status") != "ambiguous_source_ids"]
    seeds = [row["kg"].get("reference_terms") or [] for row in records]
    seeded_rows = sum(bool(terms) for terms in seeds)
    definitions = [row["kg"]["definition_evidence"] for row in records
                   if row["kg"].get("definition_evidence")]
    return {
        "placement_audits": len(audits),
        "audit_status": dict(Counter(audit["result"]["status"] for audit in audits)),
        "planner_tool": {
            **_lookup_results(executed, attempts),
            "invalid_actions": len(invalid),
            "duplicate_actions": sum((row.get("error") or "").startswith("Already attempted") for row in invalid),
            "stop_actions": counts["stopped"], "reused_actions": counts["reused"],
            "planner_failures": counts["planner_failed"],
        },
        "kg_grounding": {
            "definition_lookup": _lookup_results(definition_queries, len(definition_queries)),
            "exact_seed_inspections": _lookup_results(inspections, len(inspections)),
            "records_with_sourced_definition": len(definitions),
            "records_with_sourced_definition_fraction": _rate(len(definitions), len(records)),
            "sourced_definitions_by_database": dict(Counter(item["database"] for item in definitions)),
            "reference_terms": sum(map(len, seeds)),
            "reference_terms_by_source": dict(Counter(term["source"] for terms in seeds for term in terms)),
            "records_with_reference_seeds": seeded_rows,
            "records_with_reference_seeds_fraction": _rate(seeded_rows, len(records)),
        },
    }


def _metrics(records, edges=(), new_node_ids=()):
    outcome = Counter(row["status"] for row in records)
    reasons = Counter(row["reason"] for row in records if row["status"] in {"needs_review", "rejected"})
    mappings = [row for row in records if row.get("mapping")]
    inserted = {row["mapping"]["ontology_node_id"] for row in mappings if row["status"] == "inserted"}
    audits = [audit for row in records
              for audit in (row.get("placement") or {}).get("reference_audits", [])]
    placement = [(row.get("placement") or {}) for row in records]
    fallbacks = [row["parent_fallback"] for row in placement if row.get("parent_fallback")]
    reviewed = [_confirmed_parents(row) for row in placement]
    parents = [confirmed for confirmed, _ in reviewed]
    fallbacks_found = [lookup for row in fallbacks for lookup in row.get("lookups", [])]
    fallback_attempts = sum(row.get("lookup_attempts", 0) for row in fallbacks)
    fallback_results = Counter(row["status"] for row in fallbacks_found)
    alignment_status = Counter(
        (row.get("realignment") or row.get("verification") or {}).get("alignment", {}).get("decision_status")
        for row in mappings
    )
    return {
        "sample_size": len(records), "outcomes": dict(outcome), "needs_review_or_rejected": dict(reasons),
        "by_type": {
            kind: dict(Counter(row["status"] for row in records if row["kg"]["kind"] == kind))
            for kind in sorted({row["kg"]["kind"] for row in records})
        },
        "concepts_inserted": len(inserted), "intermediate_parents_inserted": len(set(new_node_ids) - inserted),
        "new_maps_to": len(mappings), "maps_to_in_normal_alignment": alignment_status["ALIGNED"],
        "sample_maps_to_fraction": len(mappings) / len(records) if records else None,
        "new_edges": _schema_counts(edges, inserted),
        "records_with_confirmed_biomedical_parent": sum(bool(options) for options in parents),
        "confirmed_biomedical_parent_pairs": sum(map(len, parents)),
        "records_with_confirmed_biomedical_parent_and_not_inserted": sum(
            row["status"] != "inserted" and bool(options) for row, options in zip(records, parents)
        ),
        "records_with_independently_affirmed_parent_blocked_at_scope_or_graph_validation": sum(
            bool(blocked) for _, blocked in reviewed
        ),
        "records_with_auxiliary_type_or_relation_evidence": sum(bool(row["kg"].get("auxiliary_evidence"))
                                                                for row in records),
        "ols": _ols_metrics(records, audits),
        "direct_parent_fallback": {
            "cases": len(fallbacks), "status": dict(Counter(row["status"] for row in fallbacks)),
            "record_gathering_attempts": fallback_attempts, "records_returned": fallback_results["verified"],
            "record_gathering_return_rate": _rate(fallback_results["verified"], fallback_attempts),
            "record_gathering_status": dict(fallback_results),
        },
    }


def _completed_run(path, cohort_path):
    path, cohort_path = Path(path).resolve(), Path(cohort_path).resolve()
    frozen = read_json(cohort_path)
    manifest = read_json(path / "manifest.json")
    if (manifest["status"] != "complete" or manifest["writes_to_neo4j"]
            or manifest["inputs"]["sampling"]["cohort_file"] != _file_reference(cohort_path)):
        raise ValueError(f"Run did not complete against the requested frozen cohort: {path}")
    records = read_json(path / "records.json")
    if [row["kg"]["id"] for row in records] != [row["id"] for row in frozen["nodes"]]:
        raise ValueError(f"Completed run records differ from the frozen cohort: {path}")
    graph = read_json(path / "ontology-enriched.json")["enrichment"]
    return manifest, records, _metrics(records, graph["new_edges"], graph["new_node_ids"])


def aggregate(directory, replay_run, unseen_run):
    directory = Path(directory).resolve()
    replay_manifest, replay_after, replay_metrics = _completed_run(replay_run, directory / "replay-12.json")
    unseen_manifest, unseen_after, unseen_metrics = _completed_run(unseen_run, directory / "unseen-12.json")
    for field in ("workflow_files", "model", "ontology", "supplemental_ontology_grounding_sha256",
                  "config_sha256", "articles", "ols_enabled", "obo_sources"):
        if replay_manifest[field] != unseen_manifest[field]:
            raise ValueError(f"Replay and unseen each need the same frozen workflow setting: {field}")
    replay_reference = read_json(directory / "replay-12.json")["baseline_pilot_records"]
    if _file_reference(Path(replay_reference["path"])) != replay_reference:
        raise ValueError("Historical replay baseline records changed")
    baseline_records = {r["kg"]["id"]: r for r in read_json(replay_reference["path"])}
    baseline_replay = [baseline_records[r["kg"]["id"]] for r in replay_after]
    comparison = {
        "replay": {
            "before": _metrics(baseline_replay), "after": replay_metrics,
            "record_deltas": [
                {"kg_node_id": after["kg"]["id"], "name": after["kg"]["name"],
                 "before": before["status"], "after": after["status"], "after_reason": after["reason"]}
                for before, after in zip(baseline_replay, replay_after)
            ],
        },
        "unseen": {
            "before": {"supported_coverage": 0, "enrichment_outcomes": None},
            "after": unseen_metrics,
            "records": [
                {"kg_node_id": row["kg"]["id"], "name": row["kg"]["name"],
                 "outcome": row["status"], "reason": row["reason"]}
                for row in unseen_after
            ],
        },
        "artifacts": {
            "historical_replay": replay_reference,
            "replay_after": _file_reference(Path(replay_run).resolve() / "records.json"),
            "unseen_after": _file_reference(Path(unseen_run).resolve() / "records.json"),
        },
        "scope": {
            "replay": "The same 12 recorded failures compared with a separate run from the frozen original ontology. The earlier pilot had other same-type neighbors.",
            "unseen": "The predefined unseen cohort excluded all 42 development samples. Historical supported coverage was zero; no historical enrichment outcomes or accuracy estimates exist.",
            "evaluation": "Confirmed parents require a successful review with the KG target as the child. This includes confirmed candidates held before publication. Independent biomedical quality review is separate.",
            "tool_metrics": "Rates use recorded logical attempts. A returned term or search hit is evidence availability and does not establish a verified parent.",
        },
    }
    with run_lock(directory):
        _write_frozen(directory / "comparison.json", comparison)
    return comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    freeze_parser = subcommands.add_parser("freeze")
    freeze_parser.add_argument("--pilot", type=Path, required=True)
    freeze_parser.add_argument("--directory", type=Path, required=True)
    freeze_parser.add_argument("--seed", type=int, default=20261123)
    aggregate_parser = subcommands.add_parser("aggregate")
    aggregate_parser.add_argument("--directory", type=Path, required=True)
    aggregate_parser.add_argument("--replay-run", type=Path, required=True)
    aggregate_parser.add_argument("--unseen-run", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        result = freeze(args.pilot, args.directory, seed=args.seed)
    else:
        result = aggregate(args.directory, args.replay_run, args.unseen_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
