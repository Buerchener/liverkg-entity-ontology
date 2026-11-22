"""Export a reviewed local copy. Raw model graphs, evidence, and source KG stay intact."""
import argparse
from collections import Counter
from graphlib import TopologicalSorter
from pathlib import Path
import hashlib
import json
import os


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != data:
            raise ValueError(f"Output already contains a different review: {path}")
        return
    path.write_text(data, encoding="utf-8")


def reviewed(run, decisions, output, edge_reviews=(), original=None):
    run, output = run.resolve(), output.resolve()
    rejected_edges = {(e["child_id"], e["parent_id"]): e for e in edge_reviews}
    if run == output or output.is_relative_to(run):
        raise ValueError("Keep the reviewed copy separate from the measured run")
    manifest = read(run/"manifest.json")
    assert manifest["status"] == "complete" and manifest["writes_to_neo4j"] is False
    graph, links, records = (read(run/n) for n in ("ontology-enriched.json", "alignment-enriched.json", "records.json"))
    original = original or Path(manifest["ontology"]["path"])
    assert hashlib.sha256(original.read_bytes()).hexdigest() == manifest["ontology"]["sha256"]
    baseline = read(original)
    originals = {n["id"] for n in baseline["nodes"]}
    added = set(graph["enrichment"]["new_node_ids"])
    by_kg = {r["kg"]["id"]: r for r in records}
    ref_owner = {r["record_ref"]: r["kg"]["id"] for r in records}
    owners = {}
    for r in records:
        if r["status"] != "inserted":
            continue
        placement = r["placement"]
        made = {r["mapping"]["ontology_node_id"], *placement.get("generated_parent_ids", [])}
        if placement.get("generated_parent_id"):
            made.add(placement["generated_parent_id"])
        for ident in made:
            assert ident in added and ident not in owners
            owners[ident] = r["kg"]["id"]
    assert set(owners) == added
    flagged = dict(decisions)
    assert all(kg in by_kg and by_kg[kg].get("mapping") for kg in flagged)
    while True:
        removed = {n for n, kg in owners.items() if kg in flagged}
        dependent = {}
        for kg, r in by_kg.items():
            if not r.get("mapping") or kg in flagged:
                continue
            endpoints = {r["mapping"]["ontology_node_id"]}
            for e in (r.get("placement") or {}).get("edge_evidence", []):
                endpoints.update([e["child_id"], e["parent_id"]])
            if overlap := sorted(removed & endpoints):
                dependent[kg] = {"reason": "depends_on_quarantined_node", "node_ids": overlap}
        if not dependent:
            break
        flagged.update(dependent)
    raw_ref = lambda path: os.path.relpath(run/path, output)
    demonstrated = set()
    kept = []
    for e in graph["enrichment"]["new_edges"]:
        edge = e["child_id"], e["parent_id"]
        if edge in rejected_edges:
            # Only remove an extra link to an original local class. An imported
            # endpoint or a child losing its last specific parent needs separate review.
            assert edge[0] in added and edge[1] in originals and edge[1] not in graph["roots"].values()
            assert ref_owner[e["evidence_record"]] not in flagged
            demonstrated.add(edge)
            continue
        if ref_owner[e["evidence_record"]] not in flagged:
            kept.append({**e, "evidence_record": raw_ref(e["evidence_record"])})
    assert demonstrated == set(rejected_edges)
    for child, _ in demonstrated:
        assert any(e["child_id"] == child and e["parent_id"] not in graph["roots"].values() for e in kept)
    nodes = [n for n in graph["nodes"] if n["id"] not in removed]
    ids = {n["id"] for n in nodes}
    edges = [*baseline["edges"], *kept]
    topo = TopologicalSorter()
    for e in edges:
        assert e["child_id"] in ids and e["parent_id"] in ids
        topo.add(e["child_id"], e["parent_id"])
    topo.prepare()
    mappings = []
    for m in links["new_maps_to"]:
        if m["kg_node_id"] in flagged:
            continue
        assert m["ontology_node_id"] in ids
        mappings.append({**m, "evidence": {**m["evidence"], "record": raw_ref(m["evidence"]["record"])}})
    assert all((output/e["evidence_record"]).resolve().is_file() for e in kept)
    assert all((output/m["evidence"]["record"]).resolve().is_file() for m in mappings)
    retained_ids = sorted(ids - originals)
    g = {**graph, "nodes": nodes, "edges": edges, "enrichment": {
        **graph["enrichment"], "new_node_ids": retained_ids, "new_edges": kept,
        "source_experiment": raw_ref("manifest.json"), "quality_decisions": "review-decisions.json"}}
    outcomes = Counter("needs_review" if kg in flagged else r["status"] for kg, r in by_kg.items())
    final_targets = {m["ontology_node_id"] for m in mappings if by_kg[m["kg_node_id"]]["status"] == "inserted"}
    root_ids = set(g["roots"].values())
    specific = {e["child_id"] for e in kept if e["parent_id"] not in root_ids}
    raw = read(run/"summary.json")["coverage"]
    total = raw["kg_population"]
    summary = {"outcomes": dict(outcomes), "maps_to": len(mappings), "sample_size": len(records),
               "initial_qa_flags": len(decisions), "dependent_quarantines": len(flagged)-len(decisions),
               "inserted_specific": len(final_targets & specific), "inserted_root_only": len(final_targets-specific),
               "new_intermediate_parents": len(retained_ids)-outcomes["inserted"],
               "new_edges": len(kept), "specific_edges": sum(e["parent_id"] not in root_ids for e in kept),
               "kg_population": total,
               "supported_before": raw["baseline_supported_kg_nodes"],
               "supported_after": raw["baseline_supported_kg_nodes"]+len(mappings),
               "identity_before": raw["baseline_maps_to_kg_nodes"],
               "identity_after": raw["baseline_maps_to_kg_nodes"]+len(mappings)}
    output.mkdir(parents=True,exist_ok=True)
    save(output/"ontology-enriched.json", g)
    save(output/"alignment-enriched.json", {**links,"new_maps_to":mappings})
    adjudication = {"quarantined":flagged, "initial_flags":decisions, "raw_run": raw_ref("manifest.json")}
    if rejected_edges:
        adjudication["rejected_edges"] = list(rejected_edges.values())
    save(output/"review-decisions.json", adjudication)
    save(output/"summary.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Completed run directory")
    parser.add_argument("decisions", type=Path, help="Scope decision JSON")
    parser.add_argument("output", type=Path, help="Separate reviewed output directory")
    parser.add_argument("--ontology", type=Path, help="Relocated copy of the original ontology; the recorded SHA-256 must match")
    args = parser.parse_args()
    value = read(args.decisions)
    if "rejected_edges" in value:
        result = reviewed(args.run, value["quarantined"], args.output, value["rejected_edges"], args.ontology)
    else:
        result = reviewed(args.run, value, args.output, original=args.ontology)
    print(json.dumps(result, ensure_ascii=False))
