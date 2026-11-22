"""Run and compare paired, read-only enrichment samples with isolated model journals."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import subprocess
import sys

from ..enrichment.experiment import _metrics, _write_frozen
from ..enrichment.inputs import _balanced_sample, _file_reference, load_enrichment_inputs
from ..storage import digest, norm, read_json


REPO = Path(__file__).resolve().parents[2]
DEFAULT_DIRECTORY = REPO / "runs/enrichment/evidence-v3"
BASELINE_RUNS = {"flash": "replay-flash", "plus": "replay-v3"}
COHORTS = {"replay": "replay-12.json", "unseen": "unseen-14.json", "blind": "blind-14.json"}
PRICE_SOURCE = "https://help.aliyun.com/zh/model-studio/model-pricing"
# Per million tokens on the Beijing standard synchronous API, before any account discounts.
# Each tier applies to the entire request, chosen by its total input length.
PRICE_TIERS = {
    "qwen3.7-flash": [(32 * 1024, 0.2, 0.04, 0.8), (256 * 1024, 0.6, 0.12, 2.4),
                      (1024 * 1024, 1.2, 0.24, 4.8)],
    "qwen3.7-plus": [(256 * 1024, 2.0, 0.4, 8.0), (1024 * 1024, 6.0, 1.2, 24.0)],
}


def freeze(v2, directory, seed=20261124):
    v2, directory = Path(v2).resolve(), Path(directory).resolve()
    replay, old_holdout = (read_json(v2 / name) for name in ("replay-12.json", "unseen-12.json"))
    pilot = read_json(replay["pilot_sample"]["path"])["nodes"]
    if _file_reference(Path(replay["pilot_sample"]["path"])) != replay["pilot_sample"]:
        raise ValueError("Original pilot sample has changed")
    for cohort in (replay, old_holdout):
        if len(cohort["nodes"]) != 12 or cohort["signature"] != digest(
            {k: v for k, v in cohort.items() if k != "signature"}
        ):
            raise ValueError("Invalid historical cohort")
    inputs = load_enrichment_inputs(
        replay["kg_snapshot"]["path"], [entry["path"] for entry in replay["alignment_files"]],
        cohort_path=v2 / "replay-12.json",
    )
    pilot_ids = {row["id"] for row in pilot}
    holdout_ids = {row["id"] for row in old_holdout["nodes"]}
    covered = set(inputs["baseline"]["covered_ids"])
    if len(pilot_ids) != 42 or len(holdout_ids) != 12 or pilot_ids & holdout_ids:
        raise ValueError("Historical exclusions are incomplete")
    selected, design = _balanced_sample(inputs["nodes"], covered | pilot_ids | holdout_ids, 14, seed)
    if sorted(Counter(row["kind"] for row in selected).values()) != [2] * 7:
        raise ValueError("Two nodes are required in each of the seven entity types")
    doc = {k: v for k, v in replay.items() if k not in {"cohort", "selection", "signature", "nodes"}}
    doc.update(cohort="unseen", selection={
        **design, "excluded_pilot_records": len(pilot_ids), "excluded_prior_unseen_records": len(holdout_ids),
        "excluded_prior_unseen_cohort": _file_reference(v2 / "unseen-12.json"),
        "excluded_supported_baseline_kg_nodes": len(covered),
    }, nodes=[{"id": row["id"], "kind": row["kind"]} for row in selected])
    directory.mkdir(parents=True, exist_ok=True)
    seals = {"replay": _write_frozen(directory / COHORTS["replay"], replay),
             "unseen": _write_frozen(directory / COHORTS["unseen"], {**doc, "signature": digest(doc)})}
    prior = [replay["pilot_sample"], _file_reference(v2 / "unseen-12.json"), seals["unseen"]]
    seen = pilot_ids | holdout_ids | {row["id"] for row in selected}
    if len(seen) != 68:
        raise ValueError("The second independent cohort requires all 68 previous sample exclusions")

    def keys(node):
        return {(node["kind"], norm(val)) for val in [node["name"], *node.get("aliases", []),
                                                      *node.get("standard_ids", [])] if val.strip()}

    observed_keys = set().union(*(keys(node) for node in inputs["nodes"] if node["id"] in seen))
    collisions = {node["id"] for node in inputs["nodes"] if keys(node) & observed_keys}
    blind, design = _balanced_sample(inputs["nodes"], covered | seen | collisions, 14, 20261125)
    if sorted(Counter(row["kind"] for row in blind).values()) != [2] * 7:
        raise ValueError("Two blind nodes are required in each of the seven entity types")
    blind_doc = {**doc, "selection": {
        **design, "excluded_prior_node_count": len(seen), "excluded_prior_cohort_files": prior,
        "excluded_additional_same_type_name_alias_id_collisions": len(collisions - seen - covered),
        "excluded_supported_baseline_kg_nodes": len(covered),
        "method": ("Predefined independent second holdout; exclude previously tested KG IDs and all same-type "
                   "normalized names, aliases or supplied standard IDs shared with those inputs before seeded stratification."),
    }, "nodes": [{"id": n["id"], "kind": n["kind"]} for n in blind]}
    seals["blind"] = _write_frozen(directory / COHORTS["blind"], {**blind_doc, "signature": digest(blind_doc)})
    return seals


def _require_file(ref):
    if _file_reference(Path(ref["path"])) != ref:
        raise ValueError(f"Changed or missing frozen input: {ref['path']}")
    return ref["path"]


def launch(args):
    directory, v2 = args.directory.resolve(), args.v2.resolve()
    original = read_json(v2 / BASELINE_RUNS[args.model] / "manifest.json")
    cohort = directory / COHORTS[args.cohort]
    signed = read_json(cohort)
    if signed["signature"] != digest({k: v for k, v in signed.items() if k != "signature"}):
        raise ValueError("Cohort hash does not match its contents")
    workdir = directory / "baseline-source" if args.version == "before" else REPO
    if args.version == "before":
        snapshot = read_json(workdir / "manifest.json")
        if snapshot["sha256"] != digest(snapshot["files"]):
            raise ValueError("Baseline source manifest is inconsistent")
        for item in snapshot["files"]:
            _require_file({"path": str(workdir / item["path"]), "sha256": item["sha256"]})
        for name, sha in original["workflow_files"].items():
            _require_file({"path": str(workdir / "entity_ontology" / name), "sha256": sha})
    config, grounding = args.config.resolve(), args.grounding.resolve()
    _require_file({"path": str(config), "sha256": original["config_sha256"]})
    _require_file({"path": str(grounding), "sha256": original["supplemental_ontology_grounding_sha256"]})
    name = args.output_name or f"{args.version}-{args.cohort}-{args.model}"
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,64}", name):
        raise ValueError("Use a short alphanumeric output directory name")
    output = directory / name
    argv = [sys.executable, "-m", "entity_ontology.enrichment", "--cohort", str(cohort),
            "--ontology", _require_file(signed["baseline_ontology"]),
            "--kg-snapshot", _require_file(signed["kg_snapshot"]),
            "--config", str(config), "--ontology-grounding", str(grounding), "--output", str(output),
            "--workers", str(args.workers), "--max-llm-calls", str(args.max_calls)]
    for ref in signed["alignment_files"]:
        argv.extend(["--alignment", _require_file(ref)])
    for ref in original["articles"]:
        argv.extend(["--articles", _require_file(ref)])
    for kind, obo in original["obo_sources"].items():
        option = {"CellType": "--cell-ontology", "Tissue": "--uberon", "Disease": "--mondo", "Metabolite": "--chebi"}[kind]
        argv.extend([option, _require_file({"path": obo["path"], "sha256": obo["sha256"]})])
    if args.execute:
        if not (output / "manifest.json").exists() and (output / "llm").exists():
            raise ValueError("Use a fresh journal for a new paired experiment")
        subprocess.run(argv, cwd=workdir, check=True)
    return {"working_directory": str(workdir), "command": argv, "executed": args.execute}


def _call_refs(record):
    refs = list(record.get("model_call_refs", []))
    for stage in ("verification", "realignment"):
        alignment = (record.get(stage) or {}).get("alignment") or {}
        refs.extend((alignment.get("evidence") or {}).get("model_call_refs", []))
    return set(refs)


def _usage(response, model):
    usage = response.get("usage") or {}
    counts = Counter()
    prompt, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if type(prompt) != int or type(out) != int or min(prompt, out) < 0:
        return Counter(usage_unavailable=1)
    total = usage.get("total_tokens")
    if total is not None and total != prompt + out:
        raise ValueError("Provider token counts are inconsistent")
    counts.update(prompt_tokens=prompt, completion_tokens=out, total_tokens=prompt + out, responses_with_usage=1)
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is not None and (type(cached) != int or not 0 <= cached <= prompt):
        raise ValueError("Invalid provider prefix-cache count")
    if cached is None:
        counts["responses_without_cache_breakdown"] += 1
    else:
        counts["provider_cached_input_tokens"] += cached
    tiers = PRICE_TIERS[model]
    price = next(((pin, pcache, pout) for limit, pin, pcache, pout in tiers if prompt <= limit), None)
    if price is None:
        raise ValueError("Request outside documented price tiers")
    pin, pcache, pout = price
    # If the cache breakdown is absent, cost is an interval, never an imputed hit rate.
    for side, cache_tokens in (("min", prompt), ("max", 0)):
        used_cache = cached if cached is not None else cache_tokens
        counts["list_cny_" + side] = (pin * (prompt - used_cache) + pcache * used_cache + pout * out) / 1e6
    return counts


def _group_work(entries):
    counts = Counter()
    stages = defaultdict(Counter)
    for stage, values in entries:
        counts.update(values)
        stages[stage].update(values)
    return {**dict(counts), "by_stage": {k: dict(v) for k, v in sorted(stages.items())}}


def journals(directory, records, model):
    directory = Path(directory)
    refs = {ref for row in records for ref in _call_refs(row)}
    ledger = directory / "cache-origin.json"
    imported = ({item["file"]: item["request_sha256"] for item in read_json(ledger)["model_requests"]}
                if ledger.is_file() else {})
    all_calls = {}
    for path in sorted((directory / "llm").glob("*.json")):
        item = read_json(path)
        ref = f"llm/{path.name}"
        if ref in imported and imported[ref] != item["request_sha256"]:
            raise ValueError(f"Invalid exact-response cache provenance for {ref}")
        stage = re.sub(r"-[a-f0-9]{8}$", "", item["request"]["stage"])
        response = item.get("response") or {}
        counts = _usage(response, model)
        counts["journal_requests"] += 1
        counts["status_" + item["status"]] += 1
        counts["imported_local_responses" if ref in imported else "new_journal_requests"] += 1
        all_calls[ref] = (stage, counts)
    if not (refs | imported.keys()) <= all_calls.keys():
        raise ValueError("Missing referenced journal or cache provenance entry")
    # Compare the work of the algorithm. Exclude saved responses preloaded but unused.
    # A fresh failed/unreferenced attempt still represents work performed during the run.
    fresh = all_calls.keys() - imported.keys()
    workload = refs | fresh
    return {
        "logical_work": _group_work(all_calls[k] for k in sorted(workload)),
        "fresh_provider_attempts": _group_work(all_calls[k] for k in sorted(fresh)),
        "unused_imported_responses": len(imported.keys() - refs),
        "unattributed_fresh_requests": len(fresh - refs),
        "per_node": {r["kg"]["id"]: _group_work(all_calls[k] for k in sorted(_call_refs(r)))
                     for r in records},
    }


def _completed(path, cohort_path):
    path = Path(path).resolve()
    manifest = read_json(path / "manifest.json")
    sample = manifest["inputs"]["sampling"]
    if (manifest.get("status") != "complete" or manifest.get("writes_to_neo4j") is not False
            or sample["cohort_file"]["sha256"] != _file_reference(Path(cohort_path))["sha256"]):
        raise ValueError(f"Run must be complete and reference the frozen cohort: {path}")
    frozen = read_json(cohort_path)
    records = read_json(path / "records.json")
    if [(r["kg"]["id"], r["kg"]["kind"]) for r in records] != [(n["id"], n["kind"]) for n in frozen["nodes"]]:
        raise ValueError("Ordered sample differs between completed run and frozen cohort")
    graph = read_json(path / "ontology-enriched.json")["enrichment"]
    quality = _metrics(records, graph["new_edges"], graph["new_node_ids"])
    model = manifest["model"]["parameters"]["model"]
    billing = journals(path, records, model)
    return manifest, records, quality, billing


def _outcome(row, graph_parents):
    placement = row.get("placement") or {}
    target = (row.get("mapping") or {}).get("ontology_node_id")
    parents = graph_parents.get(target, []) if row["status"] == "inserted" else []
    fallback = placement.get("parent_fallback") or {}
    fallback_reviewed = fallback.get("candidates", [])
    # Re-score both historical and new reviews with the same two axes. A
    # supported direct edge can remain an unselected or uninformative parent.
    bound_isa = [
        candidate for candidate in fallback_reviewed if candidate.get("path")
        and all(edge.get("predicate") == "is_a" and edge.get("source_url") for edge in candidate["path"])
        and (candidate.get("review") or {}).get("relation_type") == "IS_A"
        and candidate["review"].get("same_endpoint_scope") is True
    ]
    return {
        "status": row["status"], "reason": row["reason"], "stage": row.get("decision_stage"),
        "placement_action": placement.get("status"), "maps_to": bool(row.get("mapping")),
        "published_parents": sorted(parents), "specific_parent": any(not i.startswith("type:") for i in parents),
        "realignment_reason": (row.get("realignment") or {}).get("reason"),
        "staged_specific_parent": bool(placement.get("status") == "inserted" and any(
            not ident.startswith("type:") for ident in placement.get("parent_ids", []))),
        "revalidated_candidate_ids": sorted({c["term"]["term_id"] for c in bound_isa}),
        "informative_revalidated_candidate_ids": sorted({c["term"]["term_id"] for c in bound_isa
                                                         if c["review"].get("informative") is True}),
        "parent_recovery_status": fallback.get("status"),
        "alias_dispositions": _alias_dispositions(row),
    }


def _alias_dispositions(row):
    """Audit competing exact names before placement and after staging, excluding the self anchor."""
    kg = row["kg"]
    source = kg["source"]
    own_id = "KG-" + digest([source["database"], source["element_id"]])[:20]
    output = []
    for stage in ("verification", "realignment"):
        step = row.get(stage) or {}
        evidence = step.get("identity_evidence") or {}
        scopes = {s["ontology_node_id"]: s for s in evidence.get("scope_resolutions", [])}
        unresolved = set(evidence.get("unresolved_exact_matches", []))
        for match in evidence.get("exact_matches", []):
            ident = match["ontology_node_id"]
            if not match.get("shared_names") or (stage == "realignment" and ident == own_id):
                continue
            scope = scopes.get(ident, {})
            relation, basis = scope.get("relation"), scope.get("basis")
            if ident in unresolved:
                disposition = "unresolved"
            elif relation in {"IS_A", "REVERSE_IS_A"} and basis == "authority_asserted_strict_scope":
                disposition = "resolved_distinct_by_authority"
            elif step.get("action") == "existing" and step.get("ontology_node_id") == ident:
                disposition = ("resolved_equivalent_by_authority" if basis == "shared_bound_authority_term"
                               else "accepted_equivalent_by_alignment")
            elif not match["same_type"]:
                disposition = "excluded_as_different_type"
            else:
                disposition = "other_nonblocking_assessment"
            output.append({"stage": stage, "ontology_node_id": ident, "name": match["ontology_name"],
                           "shared_names": match["shared_names"], "relation": relation, "basis": basis,
                           "disposition": disposition})
    return output


def _alias_transitions(before, after):
    def candidates(outcome):
        return {(a["stage"], a["ontology_node_id"]): a["disposition"] for a in outcome["alias_dispositions"]}
    left, right = candidates(before), candidates(after)
    return [{"stage": key[0], "ontology_node_id": key[1],
             "before": left.get(key, "not_observed"), "after": right.get(key, "not_observed")}
            for key in sorted(left.keys() | right.keys())]


def _direct_evidence(row, graph):
    """Count directed facts from verified KG seeds even when no fallback/LLM accepted them."""
    placement = row.get("placement") or {}
    fallback = placement.get("parent_fallback") or {}
    seen = set()
    facts = []
    for seed in row["kg"].get("reference_terms", []):
        if not seed.get("identity_evidence") or not seed.get("source_url"):
            continue
        for edge in seed.get("parent_relations", []):
            pair = seed["term_id"], edge.get("parent_id")
            if (pair in seen or not pair[1] or pair[0] == pair[1]
                    or edge.get("child_id") != pair[0] or edge.get("predicate") != "is_a"
                    or not edge.get("source_url") or edge.get("source") != seed.get("source")):
                continue
            seen.add(pair)
            facts.append({"child": pair[0], "parent": pair[1], "predicate": "is_a",
                          "source": edge["source"], "source_url": edge["source_url"],
                          "field": edge.get("field"), "line": edge.get("line")})
    def parent(node_id):
        n = graph[node_id]
        return {"id": node_id, "name": n["name"], "standard_ids": n.get("standard_ids", [])}
    candidates = []
    for candidate in fallback.get("candidates", []):
        path = candidate.get("path") or []
        key = candidate["start"]["term_id"], candidate["term"]["term_id"]
        if len(path) == 1 and key in seen:
            candidates.append({"child": key[0], "parent": key[1], "label": candidate["term"]["name"],
                               "status": candidate["status"], "review": candidate.get("review"),
                               "preference": candidate.get("preference")})
    # Only committed placements are present in this graph. Source identities and
    # per-edge reviews are retained separately for staged placements rolled back.
    published = [parent(ident) for ident in placement.get("parent_ids", [])] if row["status"] == "inserted" else []
    return {"bound_seed_direct_is_a": facts, "examined_direct_candidates": candidates,
            "published_parents": published}


def paired(before_path, after_path, cohort_path, *, direct_evidence=False):
    runs = [Path(p).resolve() for p in (before_path, after_path)]
    exports, manifests, outcomes = [], [], []
    for run in runs:
        m, rows, quality, usage = _completed(run, cohort_path)
        manifests.append(m)
        graph = read_json(run / "ontology-enriched.json")["enrichment"]
        parents = defaultdict(list)
        for edge in graph["new_edges"]:
            parents[edge["child_id"]].append(edge["parent_id"])
        node_outcomes = [_outcome(row, parents) for row in rows]
        if direct_evidence:
            graph_nodes = {n["id"]: n for n in read_json(run / "ontology-enriched.json")["nodes"]}
            for outcome, row in zip(node_outcomes, rows):
                outcome["source_evidence"] = _direct_evidence(row, graph_nodes)
            source_ids = [o["source_evidence"] for o in node_outcomes]
            quality["nodes_with_bound_source_direct_parent_id"] = sum(bool(s["bound_seed_direct_is_a"]) for s in source_ids)
            quality["bound_source_direct_parent_facts"] = sum(len(s["bound_seed_direct_is_a"]) for s in source_ids)
            quality["distinct_direct_candidates_reviewed_or_filtered"] = sum(len(s["examined_direct_candidates"]) for s in source_ids)
        outcomes.append(node_outcomes)
        quality.update(
            nodes_with_staged_specific_parent=sum(o["staged_specific_parent"] for o in node_outcomes),
            nodes_with_published_specific_parent=sum(o["specific_parent"] for o in node_outcomes),
            nodes_with_only_published_schema_parent=sum(bool(o["published_parents"]) and not o["specific_parent"]
                                                        for o in node_outcomes),
            nodes_rolled_back_after_placement=sum(row["status"] != "inserted" and row.get("realignment") is not None
                                                   for row in rows),
            fallback_nodes_with_scope_confirmed_isa=sum(bool(o["revalidated_candidate_ids"]) for o in node_outcomes),
            fallback_nodes_with_informative_scope_confirmed_isa=sum(bool(o["informative_revalidated_candidate_ids"])
                                                                   for o in node_outcomes),
            alias_dispositions=dict(Counter(item["disposition"] for o in node_outcomes for item in o["alias_dispositions"])),
            nodes_with_unresolved_aliases=sum(any(item["disposition"] == "unresolved" for item in o["alias_dispositions"])
                                              for o in node_outcomes),
            nodes_with_authority_resolved_aliases=sum(any(item["disposition"].endswith("_by_authority")
                                                          for item in o["alias_dispositions"]) for o in node_outcomes),
        )
        # In v2 'verified' also implied 'informative'. V3 separates the two
        # flags and can reject individual parents in a multi-parent selection.
        # Report the definitive stage-supported parent count on the same basis in both arms.
        for key in ("records_with_confirmed_biomedical_parent", "confirmed_biomedical_parent_pairs",
                    "records_with_confirmed_biomedical_parent_and_not_inserted",
                    "records_with_independently_affirmed_parent_blocked_at_scope_or_graph_validation"):
            quality.pop(key)
        quality["nodes_with_staged_specific_parent_but_not_published"] = sum(
            o["staged_specific_parent"] and not o["specific_parent"] for o in node_outcomes)
        exports.append({
            "directory": str(run), "model": m["model"], "workflow_sha256": digest(m["workflow_files"]),
            "quality": quality, "usage": usage, "decision_stages": dict(Counter(
                row.get("decision_stage") or "not_logged" for row in rows)),
            "manifest_file": _file_reference(run / "manifest.json"),
            "record_file": _file_reference(run / "records.json"),
        })
    m1, m2 = manifests
    for field in ("ontology", "config_sha256", "supplemental_ontology_grounding_sha256",
                  "articles", "obo_sources", "ols_enabled"):
        if m1[field] != m2[field]:
            raise ValueError(f"Different input or provider configuration in pair: {field}")
    if m1["model"]["parameters"] != m2["model"]["parameters"] or m1["model"]["endpoint"] != m2["model"]["endpoint"]:
        raise ValueError("Paired before/after must use the same model, settings and endpoint")
    if any(m1["inputs"][k] != m2["inputs"][k] for k in ("kg_snapshot", "alignment_files")):
        raise ValueError("The pair does not share KG and baseline alignment inputs")
    before, after = exports
    old = before["usage"]["logical_work"]
    new = after["usage"]["logical_work"]
    def change(key):
        a, b = old.get(key, 0), new.get(key, 0)
        return {"before": a, "after": b, "saved": a - b, "fraction_saved": (a-b)/a if a else None}
    costs_complete = all(not x.get("usage_unavailable") and not x.get("responses_without_cache_breakdown")
                         for x in (old, new))
    names = [(r["kg"]["id"], r["kg"]["name"], r["kg"]["kind"]) for r in rows]
    return {
        "cohort": _file_reference(Path(cohort_path)), "before": before, "after": after,
        "workload_changes": {k: change(k) for k in ("journal_requests", "prompt_tokens", "completion_tokens", "total_tokens")},
        "estimated_list_cost_change": change("list_cny_max") if costs_complete else None,
        "cost_basis": {"currency": "CNY", "region": "Beijing", "pricing": PRICE_SOURCE,
                       "implicit_cache_pricing": "https://help.aliyun.com/zh/model-studio/" + m1["model"]["parameters"]["model"].replace(".", "-"),
                       "account_debit_available": False,
                       "note": "Logical work includes used exact local responses. Fresh attempts exclude them. Provider prefix-cache hits discount input only. Costs use list prices before promotions (including the Plus promotional 20% discount), credits or other account-specific charges. Missing usage is unpriced."},
        "nodes": [{"id": ident, "name": name, "kind": kind, "before": a, "after": b,
                   "alias_transitions": _alias_transitions(a, b)}
                  for (ident, name, kind), a, b in zip(names, *outcomes)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    f = sub.add_parser("freeze")
    runner = sub.add_parser("launch")
    for command in (f, runner):
        command.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
        command.add_argument("--v2", type=Path, default=DEFAULT_DIRECTORY.parent / "evidence-v2")
    runner.add_argument("--config", required=True, type=Path)
    runner.add_argument("--grounding", required=True, type=Path)
    runner.add_argument("--model", required=True, choices=BASELINE_RUNS)
    runner.add_argument("--version", required=True, choices=("before", "after"))
    runner.add_argument("--cohort", required=True, choices=COHORTS)
    runner.add_argument("--workers", type=int, default=7)
    runner.add_argument("--max-calls", type=int, default=240)
    runner.add_argument("--output-name", help="Use a separate output basename for a later frozen revision.")
    runner.add_argument("--execute", action="store_true")
    pair = sub.add_parser("pair")
    for key in ("before", "after", "cohort", "output"):
        pair.add_argument("--" + key, type=Path, required=True)
    pair.add_argument("--direct-evidence", action="store_true", help="Include all endpoint-bound source assertions per KG node.")
    args = parser.parse_args()
    if args.action == "freeze":
        result = freeze(args.v2, args.directory)
    elif args.action == "launch":
        result = launch(args)
    else:
        result = _write_frozen(args.output, paired(args.before, args.after, args.cohort, direct_evidence=args.direct_evidence))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
