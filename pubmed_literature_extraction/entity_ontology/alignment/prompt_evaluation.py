"""Frozen prompt comparison with identical inputs, gates and bounded paid calls.

This module never retrieves candidates or queries OLS. Cases must already contain
the complete frozen alignment payload, including any external_reference facts.
Raw responses and failed trials remain available; format compliance is not accuracy.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import threading
import time

from ..hierarchy.ranking_journal import ContractError, strict_json
from ..model import ProviderError, endpoint, live_provider, read_settings
from ..storage import atomic_json, digest, read_json, run_lock
from .candidates import canonical_standard_id
from .decisions import _valid_citations, admit, validate_assessments
from .pipeline import AlignmentModel


VERSION = "alignment-prompt-evaluation-v1"
POSITIVE = {"MAPS_TO", "INSTANCE_OF"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _taxon(node):
    value = node.get("taxon_id") or node.get("ncbi_taxon_id")
    return str(value).removeprefix("NCBITaxon:") if value else None


def _ids(node):
    return {value for raw in node.get("standard_ids", [])
            if (value := canonical_standard_id(raw))}


def semantic_flags(rows, payload):
    """Mechanical rule conflicts, never clinical ground truth or accuracy labels."""
    kg = payload["kg"]
    candidates = {node["id"]: node for node in payload["items"]}
    flags = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        ontology = candidates.get(row["id"])
        if ontology is None:
            flags.append({"ontology_node_id": row["id"], "flag": "unknown_candidate_id"})
            continue

        def flag(label):
            flags.append({"ontology_node_id": row["id"], "flag": label})

        relation = row.get("relation_type")
        for side, source in (("kg", kg), ("ontology", ontology)):
            level = source.get("entity_level")
            if level in ("class", "individual") and row.get(side + "_entity_level") != level:
                flag(side + "_explicit_entity_level_contradiction")
        if not isinstance(relation, str) or relation not in POSITIVE:
            continue
        if kg.get("kind") != ontology.get("kind"):
            flag("positive_type_conflict")
        kt, ot = _taxon(kg), _taxon(ontology)
        if kt and ot and kt != ot:
            flag("positive_explicit_species_conflict")
        if relation == "MAPS_TO":
            if row.get("same_scope") is False:
                flag("maps_to_declares_different_scope")
            if bool(kt) != bool(ot) and not (_ids(kg) & _ids(ontology)):
                flag("maps_to_species_scope_unknown_on_one_side")
            levels = (row.get("kg_entity_level"), row.get("ontology_entity_level"))
            if "class" in levels and "individual" in levels:
                flag("maps_to_class_individual_conflict")
        if relation == "INSTANCE_OF":
            if (kg.get("entity_level") != "individual" or not kg.get("entity_level_evidence")
                    or row.get("kg_entity_level") != "individual"
                    or row.get("ontology_entity_level") != "class"
                    or ontology.get("entity_level") == "individual"):
                flag("instance_of_individual_membership_not_established")
    return flags


def _audit_response(call, payload):
    """Preserve parsable proposals even when the whole response fails its contract."""
    result = {"call_status": call["status"], "contract_valid": False,
              "validation_errors": [], "parsed_response": None, "rows": [],
              "admitted_proposals": [], "row_audit_errors": [], "semantic_flags": [],
              "raw_relation_counts": {}, "positive_empty_evidence": 0,
              "positive_without_verified_two_sided_local_citations": 0,
              "positive_without_verified_two_sided_definitions": 0}
    if call["status"] != "response_received":
        result["validation_errors"].append(call.get("error", call["status"]))
        return result
    response = call["response"]
    try:
        parsed = strict_json(response.get("content"))
        result["parsed_response"] = parsed
    except ContractError as exc:
        result["validation_errors"].append(str(exc))
        return result
    raw_rows = parsed.get("rows", []) if isinstance(parsed, dict) else []
    rows = raw_rows if isinstance(raw_rows, list) else []
    result["rows"] = rows
    if response.get("finish_reason") != "stop":
        result["validation_errors"].append("Incomplete or non-stop LLM response: " + str(response.get("finish_reason")))
    candidates = {node["id"]: node for node in payload["items"]}
    try:
        validated = validate_assessments(parsed, list(candidates))
        for row in validated:
            _, invalid = _valid_citations(row, payload["kg"], candidates[row["id"]])
            if invalid:
                raise ContractError("Citation not found in its specified LOCAL input field")
    except ContractError as exc:
        result["validation_errors"].append(str(exc))
    result["contract_valid"] = not result["validation_errors"]
    relations = Counter()
    for row in rows:
        if not isinstance(row, dict):
            continue
        relation = row.get("relation_type")
        relations[relation if isinstance(relation, str) else "INVALID"] += 1
        valid = []
        try:
            ident = row.get("id")
            if not isinstance(ident, str) or ident not in candidates:
                raise ContractError("Unknown or invalid candidate ID")
            validate_assessments({"rows": [row]}, [ident])
            valid, _ = _valid_citations(row, payload["kg"], candidates[ident])
            # Reuse the existing unmodified admission gates, even for invalid citations.
            result["admitted_proposals"].append(admit(
                row, payload["kg"], candidates[ident], payload.get("external_reference")))
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            result["row_audit_errors"].append({"row_id": row.get("id"), "error_type": type(exc).__name__, "error": str(exc)})
        if isinstance(relation, str) and relation in POSITIVE:
            evidence = row.get("evidence")
            result["positive_empty_evidence"] += not isinstance(evidence, list) or not evidence
            result["positive_without_verified_two_sided_local_citations"] += {c["side"] for c in valid} != {"kg", "ontology"}
            definition_sides = {c["side"] for c in valid if c["field"] == "definition" and len(c["quote"].strip()) >= 20}
            result["positive_without_verified_two_sided_definitions"] += definition_sides != {"kg", "ontology"}
    result["raw_relation_counts"] = dict(relations)
    result["semantic_flags"] = semantic_flags(rows, payload)
    return result


class _Calls:
    def __init__(self, output, connection, parameters, maximum, provider):
        self.output, self.connection, self.parameters = output, connection, parameters
        self.maximum, self.provider = maximum, provider
        self.lock = threading.Lock()
        self.count = len(list((output / "calls").glob("*.json")))
        self.new_calls = self.cache_hits = 0

    def call(self, key, prompt, payload):
        body = {"model": self.parameters["model"], "temperature": self.parameters["temperature"],
                "max_tokens": self.parameters["max_tokens"], "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": prompt},
                             {"role": "user", "content": json.dumps(payload, ensure_ascii=False, allow_nan=False)}]}
        extras = {**self.connection.get("extra_body", {}), **self.parameters.get("extra_body", {})}
        if any(k in body or k in {"api_key", "authorization", "headers", "stream"} for k in extras):
            raise ValueError("extra_body cannot override required request fields")
        body.update(extras)
        request = {"stage": "alignment", "endpoint": endpoint(self.connection),
                   "body": body, "prompt_sha256": digest(prompt), "payload_sha256": digest(payload)}
        request_hash = digest(request)
        path = self.output / "calls" / (digest(key) + ".json")
        ref = str(path.relative_to(self.output))
        with self.lock:
            if path.exists():
                record = read_json(path)
                if record.get("request_hash") != request_hash:
                    raise ValueError("Saved evaluation call differs from its frozen request")
                self.cache_hits += 1
                return ref, record
            if self.count >= self.maximum:
                return None, {"status": "budget_not_called", "error": "global_call_budget_exhausted"}
            if self.provider is None:
                self.provider = live_provider(dict(self.connection), self.parameters)
            record = {"request_hash": request_hash, "request": request, "status": "request_started",
                      "started_utc": _now(), "trial": key,
                      "billing_note": "A saved interrupted request is not automatically repeated; billing may have occurred."}
            atomic_json(path, record)
            self.count += 1
            self.new_calls += 1
        started = time.monotonic()
        try:
            response = self.provider.generate("alignment", prompt, payload, self.parameters)
            record.update(status="response_received", response=response)
        except Exception as exc:
            record.update(status="provider_error", error_type=type(exc).__name__,
                          error=str(exc) if isinstance(exc, ProviderError) else type(exc).__name__)
        record.update(latency_seconds=time.monotonic() - started, completed_utc=_now())
        atomic_json(path, record)
        return ref, record


def _metrics(results):
    relations, flags, admitted = Counter(), Counter(), Counter()
    totals = Counter()
    for result in results:
        relations.update(result["raw_relation_counts"])
        flags.update(flag["flag"] for flag in result["semantic_flags"])
        for item in result["admitted_proposals"]:
            if item["accepted"]:
                admitted[item["assessment"]["relation_type"]] += 1
        totals["cases_with_semantic_rule_conflicts"] += bool(result["semantic_flags"])
        for key in ("positive_empty_evidence", "positive_without_verified_two_sided_local_citations",
                    "positive_without_verified_two_sided_definitions"):
            totals[key] += result[key]
    return {"raw_relation_counts": dict(relations), "rule_conflict_counts": dict(flags),
            "admitted_pair_proposal_counts": dict(admitted), **totals}


def _summary(trials, output, variants, calls):
    by_variant = {}
    for variant in variants:
        group = [r for r in trials if r["prompt_variant"] == variant]
        first = [r["attempts"][0] for r in group]
        final = [r["attempts"][r["final_attempt_index"]] for r in group]
        refs = {attempt["call_ref"] for r in group for attempt in r["attempts"] if attempt["call_ref"]}
        records = [read_json(output / ref) for ref in refs]
        usage = Counter()
        missing_usage = 0
        for record in records:
            values = record.get("response", {}).get("usage")
            if not isinstance(values, dict):
                missing_usage += 1
                continue
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if type(values.get(key)) is int:
                    usage[key] += values[key]
        count = len(group)
        latency = sum(r.get("latency_seconds", 0) for r in records)
        by_variant[variant] = {
            "trials": count, "first_pass_valid": sum(r["contract_valid"] for r in first),
            "first_pass_rate": sum(r["contract_valid"] for r in first) / count if count else 0,
            "post_retry_valid": sum(r["contract_valid"] for r in final),
            "post_retry_rate": sum(r["contract_valid"] for r in final) / count if count else 0,
            "retry_trials": sum(len(r["attempts"]) > 1 for r in group), "calls": len(records),
            "provider_error_calls": sum(r["status"] == "provider_error" for r in records),
            "interrupted_calls": sum(r["status"] == "request_started" for r in records),
            "budget_skipped_attempts": sum(a["call_status"] == "budget_not_called" for r in group for a in r["attempts"]),
            "tokens": dict(usage), "calls_without_usage": missing_usage,
            "latency_seconds_total": latency,
            "latency_seconds_mean_per_call": latency / len(records) if records else None,
            "first_pass": _metrics(first), "final": _metrics(final),
        }
    return {"version": VERSION, "by_prompt": by_variant, "trial_count": len(trials),
            "recorded_calls": calls.count, "new_provider_calls_this_invocation": calls.new_calls,
            "cached_calls_this_invocation": calls.cache_hits,
            "all_scheduled_attempts_executed": not any(v["budget_skipped_attempts"] for v in by_variant.values()),
            "interpretation": "Contract compliance and mechanical rule conflicts only; no expert gold standard or clinical accuracy estimate.",
            "admitted_counts_are": "Pair-level proposals, not final selected entity alignments.",
            "external_lookup": "Disabled; all reference evidence is frozen in the corpus."}


def run_evaluation(corpus, output, *, config_path=None, prompts=("legacy", "sections", "schema"),
                   workers=3, max_calls=100, repeats=1, provider=None, parameters=None,
                   connection=None, prompt_variants=None):
    """Evaluate every variant on every frozen case, then perform one shared repair.

    First attempts finish for all variants before any repair is dispatched, so a
    small call budget cannot let early variants consume the first-pass budget.
    Injectable providers support offline tests. The normal path reuses the project
    OpenAI-compatible provider with identical settings for every prompt variant.
    """
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("workers must be an integer from 1 to 4")
    if type(repeats) is not int or repeats < 1 or type(max_calls) is not int or max_calls < 1:
        raise ValueError("repeats and max_calls must be positive integers")
    corpus = read_json(corpus) if isinstance(corpus, (str, Path)) else corpus
    cases = corpus.get("cases") if isinstance(corpus, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("Corpus requires a nonempty cases list")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str) or not case["case_id"] or case["case_id"] in seen:
            raise ValueError("Every corpus case requires a unique nonempty case_id")
        seen.add(case["case_id"])
        payload = case.get("payload")
        if (not isinstance(payload, dict) or not isinstance(payload.get("kg"), dict)
                or not isinstance(payload.get("items"), list) or not payload["items"]):
            raise ValueError("Every case requires a frozen kg/items payload")
        if "output_format_correction" in payload:
            raise ValueError("Corpus must contain original first-pass payloads, not repairs")
        ids = [node.get("id") for node in payload["items"] if isinstance(node, dict)]
        if len(ids) != len(payload["items"]) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("Candidate IDs must be nonempty and unique")
    variants = list(prompts)
    if not variants or len(set(variants)) != len(variants):
        raise ValueError("Select distinct prompt variants")
    if prompt_variants is None:
        from .prompts import PROMPT_VARIANTS
        prompt_variants = PROMPT_VARIANTS
    selected = {name: prompt_variants[name] for name in variants}
    if any(not isinstance(value, str) or not value.strip() for value in selected.values()):
        raise ValueError("Prompt variants must be nonempty strings")
    trial_count = len(cases) * len(variants) * repeats
    if max_calls < trial_count:
        raise ValueError("max_calls must cover one first attempt for every case/variant/repeat")
    if config_path is not None:
        if connection is not None or parameters is not None:
            raise ValueError("Use config_path or explicit connection/parameters, not both")
        connection, parameters = read_settings(config_path, "audit_edges")
    if connection is None or parameters is None:
        raise ValueError("Supply config_path or explicit connection and parameters")
    for forbidden in ("api_key", "password", "authorization", "headers"):
        if forbidden in connection:
            raise ValueError("Credentials must be environment-only")
    settings = {"endpoint": endpoint(connection), "parameters": parameters,
                "provider_extra_body": connection.get("extra_body", {}),
                "timeout_seconds": connection.get("timeout_seconds"),
                "workers": workers, "repeats": repeats, "max_calls": max_calls,
                "response_format": {"type": "json_object"}, "prompt_variants": variants,
                "retry_policy": "one_identical_existing_AlignmentModel_correction_for_contract_failures_only"}
    code_hashes = {"evaluator": digest(Path(__file__).read_text()),
                   "decision_gates": digest(Path(inspect.getfile(admit)).read_text()),
                   "strict_json": digest(inspect.getsource(strict_json)),
                   "correction": digest(inspect.getsource(AlignmentModel._correction))}
    signature = digest({"version": VERSION, "corpus": corpus, "prompts": selected,
                        "settings": settings, "code_hashes": code_hashes})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with run_lock(output):
        manifest_path = output / "manifest.json"
        previous = read_json(manifest_path) if manifest_path.exists() else None
        if previous and previous.get("input_signature") != signature:
            raise ValueError("Frozen experiment differs: choose a new output directory")
        manifest = {"version": VERSION, "input_signature": signature, "settings": settings,
                    "code_hashes": code_hashes, "corpus_sha256": digest(corpus),
                    "prompt_sha256": {key: digest(value) for key, value in selected.items()},
                    "started_utc": previous["started_utc"] if previous else _now(), "status": "running"}
        atomic_json(manifest_path, manifest)
        if previous is None:
            atomic_json(output / "corpus.json", corpus)
            atomic_json(output / "prompts.json", selected)
        calls = _Calls(output, connection, parameters, max_calls, provider)
        scheduled = []
        # Rotate variant position deterministically to reduce scheduling-order bias.
        for repeat in range(repeats):
            for index, case in enumerate(cases):
                offset = (index + repeat) % len(variants)
                for variant in variants[offset:] + variants[:offset]:
                    scheduled.append((case, variant, repeat))

        def execute(task, prior=None):
            case, variant, repeat = task
            index = 1 if prior is not None else 0
            payload = case["payload"] if prior is None else AlignmentModel._correction(
                case["payload"], prior["attempts"][0]["validation_errors"][0])
            key = {"case_id": case["case_id"], "prompt_variant": variant, "repeat": repeat, "attempt": index}
            ref, record = calls.call(key, selected[variant], payload)
            audited = {**_audit_response(record, case["payload"]), "call_ref": ref}
            trial = prior or {"case_id": case["case_id"], "prompt_variant": variant,
                              "repeat": repeat, "payload_sha256": digest(case["payload"]), "attempts": []}
            trial["attempts"].append(audited)
            # An unissued budget-limited retry cannot erase the first response.
            trial["final_attempt_index"] = index if record["status"] != "budget_not_called" else 0
            trial["status"] = "complete" if prior or audited["contract_valid"] or record["status"] != "response_received" else "needs_contract_retry"
            path = output / "results" / (digest([variant, case["case_id"], repeat]) + ".json")
            atomic_json(path, trial)
            return trial

        with ThreadPoolExecutor(max_workers=workers) as executor:
            trials = list(executor.map(execute, scheduled))
            pending = [(task, trial) for task, trial in zip(scheduled, trials) if trial["status"] == "needs_contract_retry"]
            repaired = list(executor.map(lambda pair: execute(pair[0], pair[1]), pending))
        by_key = {(r["case_id"], r["prompt_variant"], r["repeat"]): r for r in repaired}
        trials = [by_key.get((r["case_id"], r["prompt_variant"], r["repeat"]), r) for r in trials]
        summary = _summary(trials, output, variants, calls)
        atomic_json(output / "summary.json", summary)
        atomic_json(manifest_path, {**manifest, "status": "complete", "completed_utc": _now(), "summary": summary})
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--prompts", nargs="+", default=["legacy", "sections", "schema"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args(argv)
    summary = run_evaluation(args.corpus, args.output, config_path=args.config,
                             prompts=args.prompts, workers=args.workers,
                             max_calls=args.max_calls, repeats=args.repeats)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
