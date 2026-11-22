"""Freeze independent KG cohorts and run the existing read-only enrichment CLI."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..enrichment.experiment import _write_frozen
from ..enrichment.inputs import _file_reference, load_enrichment_inputs
from ..storage import digest, norm, read_json
from .enrichment_v3 import (
    PRICE_SOURCE,
    _call_refs,
    _completed,
    _direct_evidence,
    _outcome,
    _require_file,
    _usage,
    journals,
    paired,
)

REPO = Path(__file__).resolve().parents[2]
V3 = REPO / "runs/enrichment/evidence-v3"
OUT = REPO / "runs/enrichment/evidence-v4"
COHORTS = {
    "regression": "regression-14.json",
    "tyramine": "tyramine-1.json",
    "independent": "independent-14.json",
    "scale": "scale-105.json",
    "difficult": "difficult.json",
    "scale-difficult": "scale-difficult.json",
    "confirmatory": "confirmatory-10.json",
}


def snapshot_baseline():
    base, overlay = V3 / "baseline-source", V3 / "confirmed-source"
    source = read_json(base / "manifest.json")
    certified = read_json(overlay / "manifest.json")
    if source["sha256"] != digest(source["files"]):
        raise ValueError("Invalid historical source manifest")
    dest = OUT / "v3-source"
    entries = []
    base_files = {r["path"]: r["sha256"] for r in source["files"]}
    for name, sha in certified["workflow_files"].items():
        base_files.setdefault("entity_ontology/" + name, sha)
    for name, original_sha in sorted(base_files.items()):
        changed = name in certified["changed_files"]
        sha = certified["changed_files"].get(name, original_sha)
        src = (overlay if changed else base) / name
        _require_file({"path": str(src), "sha256": sha})
        target = dest / name
        if target.exists():
            _require_file({"path": str(target), "sha256": sha})
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read_bytes())
        entries.append({"path": name, "sha256": sha})
    for name, sha in certified["workflow_files"].items():
        _require_file({"path": str(dest / "entity_ontology" / name), "sha256": sha})
    return _write_frozen(
        dest / "manifest.json",
        {
            "files": entries,
            "sha256": digest(entries),
            "workflow_digest": certified["workflow_digest"],
            "composed_from": [
                _file_reference(base / "manifest.json"),
                _file_reference(overlay / "manifest.json"),
            ],
        },
    )


def keys(node):
    return {
        (node["kind"], norm(x))
        for x in [node["name"], *node["aliases"], *node["standard_ids"]]
        if x.strip()
    }


def stratified(eligible, count, seed, reserve):
    groups = defaultdict(list)
    for row in eligible:
        groups[row["kind"]].append(row)
    pools = {
        k: iter(
            sorted(
                v, key=lambda r: hashlib.sha256(f"{seed}:{r['id']}".encode()).digest()
            )
        )
        for k, v in groups.items()
    }
    quotas = {k: max(0, len(v) - reserve) for k, v in groups.items()}
    seen, sampled, totals = set(), [], Counter()
    while len(sampled) < count:
        previous = len(sampled)
        for kind in sorted(pools):
            if totals[kind] >= quotas[kind]:
                continue
            for candidate in pools[kind]:
                identity = keys(candidate)
                if not seen.intersection(identity):
                    sampled.append(candidate)
                    seen.update(identity)
                    totals[kind] += 1
                    break
            if len(sampled) == count:
                break
        if len(sampled) == previous:
            raise ValueError("Insufficient distinct eligible KG records")
    return sampled, {
        "seed": seed,
        "eligible_by_type": dict(sorted((k, len(v)) for k, v in groups.items())),
        "sample_by_type": dict(sorted(totals.items())),
        "sample_size": count,
        "reserve_per_type_for_subsequent_cohort": reserve,
        "strategy": "Round-robin across available types in SHA-256(seed:KG ID) order; previously used same-type labels, aliases and supplied stable IDs excluded, including within each cohort.",
    }


def freeze():
    OUT.mkdir(parents=True, exist_ok=True)
    source = snapshot_baseline()
    old = V3 / "blind-14.json"
    baseline = read_json(old)
    past = [
        Path(baseline["pilot_sample"]["path"]),
        V3.parent / "evidence-v2/unseen-12.json",
        V3 / "unseen-14.json",
        old,
    ]
    prior = {r["id"] for path in past for r in read_json(path)["nodes"]}
    if len(prior) != 82 or baseline["signature"] != digest(
        {k: v for k, v in baseline.items() if k != "signature"}
    ):
        raise ValueError("Historical exclusions or frozen regression cohort changed")
    cohort = _write_frozen(OUT / COHORTS["regression"], baseline)
    inputs = load_enrichment_inputs(
        baseline["kg_snapshot"]["path"],
        [r["path"] for r in baseline["alignment_files"]],
        cohort_path=old,
    )
    covered = set(inputs["baseline"]["covered_ids"])
    typenames = {node["kind"] for node in inputs["nodes"]}
    common = {
        k: v
        for k, v in baseline.items()
        if k not in {"signature", "selection", "nodes", "cohort"}
    }

    def save(name, selected, selection):
        cols = [{"id": r["id"], "kind": r["kind"]} for r in selected]
        signed = {
            **common,
            "cohort": "unseen" if name in {"independent", "scale"} else "replay",
            "selection": selection,
            "nodes": cols,
        }
        return _write_frozen(
            OUT / COHORTS[name], {**signed, "signature": digest(signed)}
        )

    tr = [
        r["kg"]
        for r in read_json(V3 / "confirmed-replay-flash/records.json")
        if norm(r["kg"]["name"]) == "tyramine"
    ]
    if len(tr) != 1 or tr[0]["id"] not in prior:
        raise ValueError("Expected the original Tyramine regression record")
    seals = {
        "v3_source": source,
        "regression": cohort,
        "tyramine": save("tyramine", tr, {"basis": "historical root-evidence audit"}),
    }
    for name, count, seed, reserve in [
        ("independent", 14, 20261126, 1),
        ("scale", 105, 20261127, 0),
    ]:
        excluded_keys = set().union(
            *(keys(r) for r in inputs["nodes"] if r["id"] in prior)
        )
        eligible = [
            r
            for r in inputs["nodes"]
            if r["id"] not in prior | covered and not (keys(r) & excluded_keys)
        ]
        selected, design = stratified(eligible, count, seed, reserve)
        if set(design["sample_by_type"]) != typenames:
            raise ValueError("Frozen cohort must include every KG entity type")
        design.update(
            excluded_prior_node_count=len(prior),
            excluded_supported_node_count=len(covered),
            prior_cohorts=[_file_reference(path) for path in past],
        )
        seals[name] = save(name, selected, design)
        past.append(OUT / COHORTS[name])
        prior.update(r["id"] for r in selected)
    return seals


def freeze_confirmatory():
    original = read_json(OUT / COHORTS["scale"])
    earlier = [Path(row["path"]) for row in original["selection"]["prior_cohorts"]]
    earlier.append(OUT / COHORTS["scale"])
    seen = {r["id"] for path in earlier for r in read_json(path)["nodes"]}
    if len(seen) != 201:
        raise ValueError(
            "Confirmatory sampling requires all 201 prior exposed or reserved nodes"
        )
    inputs = load_enrichment_inputs(
        original["kg_snapshot"]["path"],
        [r["path"] for r in original["alignment_files"]],
        cohort_path=OUT / COHORTS["regression"],
    )
    covered = set(inputs["baseline"]["covered_ids"])
    prior_keys = set().union(
        *(keys(n) for n in inputs["nodes"] if n["id"] in seen | covered)
    )
    eligible = [
        n
        for n in inputs["nodes"]
        if n["id"] not in seen | covered and not (keys(n) & prior_keys)
    ]
    selection, design = stratified(eligible, 10, 20261128, 0)
    if dict(Counter(n["kind"] for n in selection)) != {
        k: 2 for k in ("CellType", "Gene", "Metabolite", "Pathway", "Protein")
    }:
        raise ValueError("Expected two independent nodes in each remaining novel type")
    design.update(
        prior_cohorts=[_file_reference(path) for path in earlier],
        excluded_prior_node_count=len(seen),
        excluded_supported_node_count=len(covered),
        exhausted_types=["Tissue", "Disease"],
    )
    doc = {
        k: v
        for k, v in original.items()
        if k not in {"selection", "nodes", "signature"}
    }
    doc.update(
        selection=design, nodes=[{"id": n["id"], "kind": n["kind"]} for n in selection]
    )
    return _write_frozen(
        OUT / COHORTS["confirmatory"], {**doc, "signature": digest(doc)}
    )


def snapshot_refinement():
    base, first, patch = OUT / "v3-source", OUT / "v4-source", OUT / "target-row-patch"
    baseline = read_json(base / "manifest.json")
    v4 = read_json(first / "manifest.json")
    original = read_json(OUT / "after-scale-flash" / "manifest.json")
    if (
        digest(baseline["files"]) != baseline["sha256"]
        or v4["workflow_files"] != original["workflow_files"]
    ):
        raise ValueError(
            "Original sealed enrichment source does not match first scale run"
        )
    overrides = {
        p.relative_to(patch).as_posix(): p
        for p in patch.rglob("*.py")
        if "__pycache__" not in p.parts
    }
    target = "entity_ontology/enrichment/pipeline.py"
    if set(overrides) != {target}:
        raise ValueError(
            "Only the scoped target-row envelope patch is part of this refinement"
        )
    files = {r["path"]: r["sha256"] for r in baseline["files"]}
    files.update(v4["changed_files"])
    workflow = dict(v4["workflow_files"])
    dest = OUT / "refined-source"
    entries = []
    for name, expected in sorted(files.items()):
        src = (first if name in v4["changed_files"] else base) / name
        _require_file({"path": str(src), "sha256": expected})
        if name in overrides:
            src = overrides[name]
            expected = _file_reference(src)["sha256"]
            workflow[name.removeprefix("entity_ontology/")] = expected
        dst = dest / name
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
        _require_file({"path": str(dst), "sha256": expected})
        entries.append({"path": name, "sha256": expected})
    for name, expected in workflow.items():
        _require_file(
            {"path": str(dest / "entity_ontology" / name), "sha256": expected}
        )
    return _write_frozen(
        dest / "manifest.json",
        {
            "files": entries,
            "sha256": digest(entries),
            "workflow_files": workflow,
            "workflow_digest": digest(workflow),
            "original_scale_workflow": digest(v4["workflow_files"]),
            "composed_from": [
                _file_reference(base / "manifest.json"),
                _file_reference(first / "manifest.json"),
                _file_reference(overrides[target]),
            ],
        },
    )


def prepare_exact_replay(origin, output, cohort, model, refined):
    """Copy only source-response caches and exact provider journals into a fresh graph."""
    origin = Path(origin).resolve()
    m = read_json(origin / "manifest.json")
    baseline = read_json(OUT / "after-scale-flash/manifest.json")
    if (
        origin == output
        or m["status"] != "complete"
        or m["writes_to_neo4j"] is not False
    ):
        raise ValueError("Warm replay needs a separate complete read-only origin")
    if (
        m["model"]["parameters"]["model"] != "qwen3.7-" + model
        or m["inputs"]["sampling"]["cohort_file"] != _file_reference(cohort)
        or m["workflow_files"] != baseline["workflow_files"]
    ):
        raise ValueError(
            "Cache model, cohort, or original workflow differs from the frozen comparison"
        )
    source = read_json(refined / "manifest.json")
    changes = {
        name
        for name in set(m["workflow_files"]) | set(source["workflow_files"])
        if m["workflow_files"].get(name) != source["workflow_files"].get(name)
    }
    if changes != {"enrichment/pipeline.py"}:
        raise ValueError(
            "Source caches may only be shared across the frozen response-envelope refinement"
        )
    ledger = output / "cache-origin.json"
    source_manifest = _file_reference(origin / "manifest.json")
    if ledger.exists():
        metadata = read_json(ledger)
        if metadata["source_manifest"] != source_manifest or metadata[
            "refined_manifest"
        ] != _file_reference(refined / "manifest.json"):
            raise ValueError("Output already has another replay origin")
        # Running/resumed copies may have additional requests or new independent
        # source responses. Preserve the normal pipeline's journal in that case.
        return
    if (output / "manifest.json").exists():
        raise ValueError("Cannot warm an already started standalone run")

    def copy_file(src):
        rel = src.relative_to(origin)
        dst = output / rel
        body = src.read_bytes()
        if dst.exists() and dst.read_bytes() != body:
            raise ValueError(f"Resume would overwrite a different cache file at {rel}")
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Publish a complete copy; never let evidence or a response remain a torn JSON file.
            with NamedTemporaryFile(dir=dst.parent, delete=False) as temp:
                temp.write(body)
            os.replace(temp.name, dst)
        return {"file": rel.as_posix(), "sha256": hashlib.sha256(body).hexdigest()}

    requests = []
    for src in sorted((origin / "llm").glob("*.json")):
        item = read_json(src)
        if item["status"] not in {
            "response_received",
            "response_invalid",
        } or not item.get("response"):
            continue
        if (
            digest(item["request"]) != item["request_sha256"]
            or item["request"]["parameters"]["model"] != "qwen3.7-" + model
        ):
            raise ValueError("Original request fingerprint or model mismatch")
        requests.append({**copy_file(src), "request_sha256": item["request_sha256"]})
    if {p.name for p in (output / "llm").glob("*.json")} != {
        Path(r["file"]).name for r in requests
    }:
        raise ValueError("Replay output has unexplained provider responses")
    cached_sources = [
        copy_file(src) for src in sorted((origin / "sources").rglob("*.json"))
    ]
    _write_frozen(
        ledger,
        {
            "source_manifest": source_manifest,
            "refined_manifest": _file_reference(refined / "manifest.json"),
            "cohort": _file_reference(cohort),
            "model_requests": requests,
            "source_caches": cached_sources,
            "reuse_policy": "Exact original provider request hash (prompt, payload and settings); response bytes unedited. Per-node records, graph, alignments and model-driven OLS decisions are recomputed.",
            "billing": "Only fresh provider attempts are additional estimated cost. Logical workload includes used preloaded responses and fresh attempts, excludes unused preloaded responses.",
        },
    )


def launch(name, version, model, outname, execute, maximum_calls, warm_from=None):
    cohort = OUT / COHORTS[name]
    signed = read_json(cohort)
    if signed["signature"] != digest(
        {k: v for k, v in signed.items() if k != "signature"}
    ):
        raise ValueError("Invalid signed cohort")
    if Path(outname).name != outname or outname in {"", ".", ".."}:
        raise ValueError("Output name must be a directory basename")
    d = OUT / outname
    manifest = read_json(V3 / ("confirmed-blind-" + model) / "manifest.json")
    code = OUT / "v3-source" if version == "before" else REPO
    if version == "before":
        source = read_json(code / "manifest.json")
        if digest(source["files"]) != source["sha256"] or source[
            "workflow_digest"
        ] != digest(manifest["workflow_files"]):
            raise ValueError("Baseline runtime differs from historical v3")
        for r in source["files"]:
            _require_file({"path": str(code / r["path"]), "sha256": r["sha256"]})
    if version == "refined":
        code = OUT / "refined-source"
        snapshot = read_json(code / "manifest.json")
        if snapshot["sha256"] != digest(snapshot["files"]) or snapshot[
            "workflow_digest"
        ] != digest(snapshot["workflow_files"]):
            raise ValueError("Invalid refined runtime snapshot")
        for r in snapshot["files"]:
            _require_file({"path": str(code / r["path"]), "sha256": r["sha256"]})
    config = (
        V3 / "model-config-plus.json"
        if model == "plus"
        else V3.parent / "evidence-v2/model-config-flash.json"
    )
    grounding = REPO / "runs/alignment/disease-protein-grounding/ontology-enriched.json"
    argv = [
        sys.executable,
        "-m",
        "entity_ontology.enrichment",
        "--cohort",
        str(cohort),
        "--ontology",
        _require_file(signed["baseline_ontology"]),
        "--kg-snapshot",
        _require_file(signed["kg_snapshot"]),
        "--config",
        _require_file({"path": str(config), "sha256": manifest["config_sha256"]}),
        "--ontology-grounding",
        _require_file(
            {
                "path": str(grounding),
                "sha256": manifest["supplemental_ontology_grounding_sha256"],
            }
        ),
        "--output",
        str(d),
        "--workers",
        "7",
        "--max-llm-calls",
        str(maximum_calls),
    ]
    for ref in signed["alignment_files"]:
        argv.extend(["--alignment", _require_file(ref)])
    for ref in manifest["articles"]:
        argv.extend(["--articles", _require_file(ref)])
    for kind, obo in manifest["obo_sources"].items():
        option = {
            "CellType": "--cell-ontology",
            "Tissue": "--uberon",
            "Disease": "--mondo",
            "Metabolite": "--chebi",
        }[kind]
        argv.extend(
            [option, _require_file({"path": obo["path"], "sha256": obo["sha256"]})]
        )
    if execute:
        env = os.environ.copy()
        if not env.get("LIVERKG_API_KEY"):
            raise ValueError("Set LIVERKG_API_KEY to run the model experiment")
        if env.get("LIVERKG_API_BASE", "").rstrip("/") != manifest["model"]["endpoint"].rstrip("/"):
            raise ValueError("Set LIVERKG_API_BASE to the endpoint recorded in the experiment manifest")
        if warm_from:
            if version != "refined":
                raise ValueError(
                    "A warm comparison must use the frozen refined runtime"
                )
            prepare_exact_replay(warm_from, d, cohort, model, code)
        elif not (d / "manifest.json").exists() and (d / "llm").exists():
            raise ValueError("A fresh run must use an empty journal")
        with (OUT / (outname + ".log")).open("a") as log:
            subprocess.run(
                argv,
                cwd=code,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
    return {
        "working_directory": str(code),
        "command": argv,
        "output": str(d),
        "executed": execute,
    }


def seal_difficult(run, maximum):
    """Select an explicitly conditional Plus diagnostic sample from a completed Flash run."""
    run = Path(run).resolve()
    manifest = read_json(run / "manifest.json")
    if (
        manifest["status"] != "complete"
        or manifest["model"]["parameters"]["model"] != "qwen3.7-flash"
    ):
        raise ValueError("Select difficult cases from a complete Flash run")
    cohort = Path(_require_file(manifest["inputs"]["sampling"]["cohort_file"]))
    _, records, _, _ = _completed(run, cohort)
    name = "scale-difficult" if cohort == OUT / COHORTS["scale"] else "difficult"
    if name == "scale-difficult" and maximum > 3:
        raise ValueError(
            "Scale Plus diagnostics are limited to three separately reported cases"
        )
    if not 1 <= maximum <= 12:
        raise ValueError("Diagnostic sample size must be between one and twelve")
    eligible = [
        r
        for r in records
        if r["status"] in {"needs_review", "rejected"}
        and r["kg"].get("definition_evidence")
        and not r["kg"]["definition_evidence"].get("scope_review_required")
        and r.get("decision_stage")
        in {"identity_verification", "hierarchy_placement", "identity_realignment"}
    ]
    # Preserve source order within a stage and interleave stages to avoid selecting
    # only the first long group of similar hierarchy complaints.
    groups = defaultdict(list)
    for r in eligible:
        groups[r["decision_stage"]].append(r)
    rows = []
    for index in range(maximum):
        for stage in sorted(groups):
            if index < len(groups[stage]) and len(rows) < maximum:
                rows.append(groups[stage][index])
    if not rows:
        return _write_frozen(
            OUT / (name + "-empty.json"),
            {
                "selected": 0,
                "source_records": _file_reference(run / "records.json"),
                "maximum": maximum,
            },
        )
    doc = read_json(cohort)
    doc.pop("signature")
    doc.update(
        cohort="replay",
        selection={
            "source_records": _file_reference(run / "records.json"),
            "source_cohort": _file_reference(cohort),
            "eligible": len(eligible),
            "maximum": maximum,
            "method": "Unmapped with sourced definitions; round robin across identity and hierarchy stages, preserving source order within stage. Separate Plus diagnostic graph; do not add its counts to Flash.",
        },
        nodes=[{"id": r["kg"]["id"], "kind": r["kg"]["kind"]} for r in rows],
    )
    return _write_frozen(OUT / COHORTS[name], {**doc, "signature": digest(doc)})


def warm_usage(run, rows, model):
    """Count the exact failed responses used to restore an alignment's correction state."""
    imported = read_json(run / "cache-origin.json")["model_requests"]
    invalid = {}
    for ref in sorted(imported, key=lambda r: r["file"]):
        call = read_json(run / ref["file"])
        if (
            call["status"] == "response_invalid"
            and call["request"]["stage"] == "alignment"
        ):
            invalid[call["request_sha256"]] = (ref["file"], call["validation_error"])
    by_case = []
    contexts = set()
    for row in rows:
        extra = set()
        refs = _call_refs(row)
        for ref in refs:
            req = read_json(run / ref)["request"]
            if (
                req["stage"] != "alignment"
                or "output_format_correction" not in req["input"]
            ):
                continue
            payload = dict(req["input"])
            correction = payload.pop("output_format_correction")
            prior = invalid.get(digest({**req, "input": payload}))
            if prior and correction.startswith(
                prior[1] + ". Return every candidate once"
            ):
                extra.add(prior[0])
        contexts.update(extra - refs)
        by_case.append({**row, "model_call_refs": sorted(refs | extra)})
    usage = journals(run, by_case, model)
    usage["prior_invalid_alignment_responses_used_for_correction"] = sorted(contexts)
    return usage


def cost_comparison(before, after, cohort):
    # The historical pair report counts refs attached to exported rows. The
    # explicit warm accounting here also includes a consulted cached correction.
    measurements = [measure(p, cohort) for p in (before, after)]
    fields = (
        "journal_requests",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "provider_cached_input_tokens",
        "list_cny_min",
        "list_cny_max",
        "usage_unavailable",
    )
    runs = []
    for metrics in measurements:
        runs.append(
            {
                "run_manifest": metrics["manifest"],
                "counts": metrics["counts"],
                "usage": {
                    kind: {f: metrics["usage"][kind].get(f, 0) for f in fields}
                    for kind in ("logical_work", "fresh_provider_attempts")
                },
                "consulted_cached_corrections": metrics["usage"].get(
                    "prior_invalid_alignment_responses_used_for_correction", []
                ),
            }
        )
    return {
        "cohort": _file_reference(cohort),
        "runs": runs,
        "note": "Fresh estimates exclude imported local journals. Logical estimates include referenced raw responses, exactly matched prior failures used to construct an alignment correction, and all fresh attempts. CNY list estimates do not represent account charges.",
    }


def measure(run, cohort):
    """Count retrieval, successful staging and post-alignment publication separately."""
    run = Path(run).resolve()
    manifest, rows, _, usage = _completed(run, cohort)
    if (run / "cache-origin.json").is_file():
        usage = warm_usage(run, rows, manifest["model"]["parameters"]["model"])
    graph = read_json(run / "ontology-enriched.json")
    nodes = {n["id"]: n for n in graph["nodes"]}
    roots = set(graph["roots"].values())
    parents = defaultdict(list)
    for edge in graph["enrichment"]["new_edges"]:
        parents[edge["child_id"]].append(edge["parent_id"])

    per_case = []
    for row in rows:
        placement = row.get("placement") or {}
        proposal = placement.get("proposal") or {}
        outcome = _outcome(row, parents)
        evidence = _direct_evidence(row, nodes)
        sources = evidence["bound_seed_direct_is_a"]
        fallback = (placement.get("parent_fallback") or {}).get("candidates", [])
        proposed = sorted(set(proposal.get("parent_ids", [])) - roots)
        # A proposed source/local parent can be unsuitable: this is retrieval,
        # deliberately distinct from either structural/semantic gate or export.
        discovered = bool(
            sources or fallback or proposed or proposal.get("generated_parent_proposal")
        )
        if outcome["staged_specific_parent"] and not discovered:
            raise ValueError(
                "A confirmed specific parent has no discoverable candidate"
            )
        if outcome["specific_parent"] and not outcome["staged_specific_parent"]:
            raise ValueError("Published parent must first pass the placement stage")
        definition = row["kg"].get("definition_evidence") or {}
        per_case.append(
            {
                "kg_id": row["kg"]["id"],
                "kind": row["kg"]["kind"],
                "name": row["kg"]["name"],
                "status": row["status"],
                "reason": row["reason"],
                "decision_stage": row.get("decision_stage"),
                "record": _file_reference(run / row["record_ref"]),
                "definition_source": definition.get("database"),
                "definition_scope_review": bool(
                    definition.get("scope_review_required")
                ),
                "retrieved_parent_candidate": discovered,
                "source_direct_parents": sources,
                "proposed_local_parent_ids": proposed,
                "verified_specific_parent": outcome["staged_specific_parent"],
                "published_specific_parent": outcome["specific_parent"],
                "published_parents": evidence["published_parents"],
                "published_root_only": bool(outcome["published_parents"])
                and not outcome["specific_parent"],
                "existing_identity_reused": row["status"] == "mapped_existing",
                "maps_to": outcome["maps_to"],
            }
        )

    def counts(group):
        return {
            "nodes": len(group),
            **{
                field: sum(bool(r[field]) for r in group)
                for field in (
                    "retrieved_parent_candidate",
                    "source_direct_parents",
                    "verified_specific_parent",
                    "published_specific_parent",
                    "published_root_only",
                    "existing_identity_reused",
                    "maps_to",
                )
            },
            "inserted": sum(r["status"] == "inserted" for r in group),
            "sourced_definitions": sum(bool(r["definition_source"]) for r in group),
            "outcomes": dict(Counter(r["status"] for r in group)),
            "reasons_for_nonpublication": dict(
                Counter(r["reason"] for r in group if not r["maps_to"])
            ),
        }

    stats = counts(per_case)
    summary = read_json(run / "summary.json")
    if (
        stats["maps_to"] != summary["new_maps_to"]
        or stats["inserted"] != summary["new_kg_concepts"]
    ):
        raise ValueError(
            "Independent counts differ from the exported enrichment summary"
        )
    return {
        "run": str(run),
        "manifest": _file_reference(run / "manifest.json"),
        "workflow_digest": digest(manifest["workflow_files"]),
        "cohort": _file_reference(Path(cohort)),
        "model": manifest["model"]["parameters"]["model"],
        "nodes": per_case,
        "counts": stats,
        "by_type": {
            kind: counts([r for r in per_case if r["kind"] == kind])
            for kind in sorted({r["kind"] for r in per_case})
        },
        "coverage": summary["coverage"],
        "new_intermediate_parents": summary["new_intermediate_parents"],
        "source_of_new_edges": summary["new_edge_support"],
        "new_edges": summary["is_a_after"] - summary["is_a_before"],
        "usage": {k: v for k, v in usage.items() if k != "per_node"},
        "rate_denominators": {
            "specific_connection_rate": "published_specific_parent / all sample nodes",
            "root_only_rate": "published_root_only / inserted KG nodes; exclude intermediate categories and mapped_existing",
        },
        "retrieval_definition": "At least one source-bound direct IS_A ID, fallback candidate, or proposed non-root local/generated parent. This counts proposals before validity and scope checks.",
        "verification_definition": "A concrete non-root parent is present on the staged DAG after semantic, evidence and structural checks; later re-alignment may veto publication.",
        "cost_basis": {
            "currency": "CNY",
            "type": "standard list-price estimate; observed prompt-cache tokens, pre-discount",
            "region": "Beijing",
            "source": "https://help.aliyun.com/zh/model-studio/model-pricing",
        },
    }


def cost_ledger():
    """Reconcile every post-v3 billed response by provider receipt across experiment directories."""
    history = [
        (V3 / ("confirmed-blind-" + model), OUT / COHORTS["regression"])
        for model in ("flash", "plus")
    ]
    runs = [
        ("before-independent-flash", "independent"),
        ("after-regression-flash", "regression"),
        ("after-difficult-plus", "difficult"),
        ("after-tyramine-flash", "tyramine"),
        ("after-independent-flash", "independent"),
        ("after-scale-flash", "scale"),
        ("refined-confirmatory-flash", "confirmatory"),
        ("refined-regression-flash", "regression"),
        ("refined-independent-flash", "independent"),
        ("refined-scale-flash", "scale"),
    ]
    empty = OUT / "scale-difficult-empty.json"
    if not empty.exists():
        runs.append(("refined-scale-difficult-plus", "scale-difficult"))
    units = (
        "journal_requests",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "provider_cached_input_tokens",
        "list_cny_min",
        "list_cny_max",
        "usage_unavailable",
    )

    def summarize(report, which):
        return {key: report["usage"][which].get(key, 0) for key in units}

    historical = []
    seen = set()
    rows = []
    all_runs = [(p, c, True) for p, c in history] + [
        (OUT / n, OUT / COHORTS[c], False) for n, c in runs
    ]
    for path, cohort, old in all_runs:
        m = measure(path, cohort)
        fresh = summarize(m, "fresh_provider_attempts")
        references = (
            read_json(path / "cache-origin.json")["model_requests"]
            if (path / "cache-origin.json").exists()
            else []
        )
        imported = {r["file"] for r in references}
        provider_ids = set()
        counted = Counter()
        no_receipt = []
        for f in sorted((path / "llm").glob("*.json")):
            if f.relative_to(path).as_posix() in imported:
                continue
            item = read_json(f)
            response = item.get("response") or {}
            receipt = (response.get("provider_response") or {}).get("id")
            if receipt:
                if receipt in seen or receipt in provider_ids:
                    raise ValueError(
                        "Duplicate provider receipt after removing imported journals: "
                        + str(f)
                    )
                provider_ids.add(receipt)
            else:
                no_receipt.append(f.name)
            counted.update(_usage(response, m["model"]))
            counted["journal_requests"] += 1
        for key in units:
            if abs(counted[key] - fresh[key]) > 1e-7:
                raise ValueError(
                    "Fresh provider records and measured usage disagree: " + key
                )
        seen.update(provider_ids)
        record = {
            "run": path.name,
            "model": m["model"],
            "manifest": m["manifest"],
            "logical": summarize(m, "logical_work"),
            "fresh": fresh,
            "distinct_provider_receipts": len(provider_ids),
            "receipt_set_sha256": digest(sorted(provider_ids)),
            "attempts_without_receipt": no_receipt,
        }
        (historical if old else rows).append(record)

    def totals(selected):
        return {
            m: {
                key: sum(r["fresh"][key] for r in selected if r["model"] == m)
                for key in units
            }
            for m in sorted({r["model"] for r in selected})
        }

    def total_all(selected):
        return {key: sum(r["fresh"][key] for r in selected) for key in units}

    scale_names = {"after-scale-flash", "refined-scale-flash"}
    return {
        "currency": "CNY",
        "price_source": PRICE_SOURCE,
        "model_reference": {
            "qwen3.7-flash": "https://help.aliyun.com/zh/model-studio/qwen3-7-flash",
            "qwen3.7-plus": "https://help.aliyun.com/zh/model-studio/qwen3-7-plus",
        },
        "region": "Beijing, standard synchronous API",
        "is_account_invoice": False,
        "note": "Logical workload prices all used raw responses at their actual model's input, provider-prefix-cache and output rates. Fresh excludes locally imported responses. Provider receipt IDs are checked for duplicates across all listed runs. Conditional Plus was run in a separate graph; do not add its outcomes to Flash.",
        "historical_v3_regression_excluded_from_current_totals": historical,
        "current_runs": rows,
        "actual_new_work_by_model": totals(rows),
        "actual_new_work_total": total_all(rows),
        "scale_105_both_design_iterations_actual_new_work": total_all(
            [r for r in rows if r["run"] in scale_names]
        ),
        "scale_105_final_workflow_logical_workload": next(
            r["logical"] for r in rows if r["run"] == "refined-scale-flash"
        ),
        "scale_105_final_replay_extra_work": next(
            r["fresh"] for r in rows if r["run"] == "refined-scale-flash"
        ),
        "conditional_scale_plus_separate": next(
            (r for r in rows if r["run"] == "refined-scale-difficult-plus"), None
        ),
        "zero_eligible_scale_diagnostic": _file_reference(empty)
        if empty.exists()
        else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("freeze")
    sub.add_parser("freeze-confirmatory")
    sub.add_parser("freeze-refinement")
    job = sub.add_parser("launch")
    job.add_argument("--cohort", choices=COHORTS, required=True)
    job.add_argument(
        "--version", choices=["before", "after", "refined"], default="after"
    )
    job.add_argument("--model", choices=["flash", "plus"], default="flash")
    job.add_argument("--output-name")
    job.add_argument("--max-calls", type=int)
    job.add_argument("--execute", action="store_true")
    job.add_argument(
        "--warm-from",
        type=Path,
        help="Completed same-cohort Flash/Plus run with exact model journals and source-response caches",
    )
    select = sub.add_parser("difficult")
    select.add_argument("--run", type=Path, required=True)
    select.add_argument("--maximum", type=int, default=5)
    audit = sub.add_parser("measure")
    audit.add_argument("--run", type=Path, required=True)
    audit.add_argument("--cohort", choices=COHORTS, required=True)
    pair = sub.add_parser("pair")
    pair.add_argument("--before", type=Path, required=True)
    pair.add_argument("--after", type=Path, required=True)
    pair.add_argument("--cohort", choices=COHORTS, required=True)
    sub.add_parser("cost-ledger")
    args = parser.parse_args()
    if args.action == "cost-ledger":
        result = _write_frozen(OUT / "api-cost-ledger.json", cost_ledger())
    elif args.action == "measure":
        result = _write_frozen(
            OUT / ("metrics-" + args.run.resolve().name + ".json"),
            measure(args.run, OUT / COHORTS[args.cohort]),
        )
    elif args.action == "pair":
        if (args.after / "cache-origin.json").is_file():
            _write_frozen(
                OUT / ("compare-workload-" + args.after.resolve().name + ".json"),
                cost_comparison(args.before, args.after, OUT / COHORTS[args.cohort]),
            )
        result = _write_frozen(
            OUT / ("compare-" + args.after.resolve().name + ".json"),
            paired(
                args.before,
                args.after,
                OUT / COHORTS[args.cohort],
                direct_evidence=True,
            ),
        )
    elif args.action == "difficult":
        result = seal_difficult(args.run, args.maximum)
    elif args.action == "freeze-refinement":
        result = snapshot_refinement()
    elif args.action == "freeze-confirmatory":
        result = freeze_confirmatory()
    elif args.action == "freeze":
        result = freeze()
    else:
        result = launch(
            args.cohort,
            args.version,
            args.model,
            args.output_name or f"{args.version}-{args.cohort}-{args.model}",
            args.execute,
            args.max_calls or (1440 if args.cohort == "scale" else 260),
            args.warm_from,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
