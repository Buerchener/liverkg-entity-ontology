"""Load a frozen KG and its supported alignment edges without querying Neo4j."""

from collections import Counter, defaultdict
import hashlib
from pathlib import Path

from ..alignment.loading import load_kg_snapshot
from ..storage import digest, read_json

IDENTITY = "MAPS_TO"
STRICT_RELATIONS = {IDENTITY, "INSTANCE_OF"}


def _file_reference(path):
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _node_id(row, nodes):
    node_id = row.get("kg_node_id")
    if not isinstance(node_id, str) or node_id not in nodes:
        raise ValueError("Alignment record has a KG node ID absent from the snapshot")
    node = nodes[node_id]
    if row.get("entity_type", node["kind"]) != node["kind"]:
        raise ValueError(f"Alignment type differs from the KG snapshot for {node_id}")
    if row.get("kg_name", node["name"]) != node["name"]:
        raise ValueError(f"Alignment name differs from the KG snapshot for {node_id}")
    return node_id


def _load_alignment(paths, nodes):
    edges = {}
    assessments = {}
    prior_grounding = defaultdict(list)
    inputs = []
    for path in paths:
        path = Path(path)
        artifact = read_json(path)
        if not isinstance(artifact, dict) or not (
            {"rows", "confirmed", "secondary_edges"} & artifact.keys()
        ):
            raise ValueError(f"Alignment artifact has no rows, confirmed, or secondary_edges: {path}")
        source = _file_reference(path)
        inputs.append(source)
        screened = artifact.get("screened_rows", [])
        if not isinstance(screened, list) or any(not isinstance(row, dict) for row in screened):
            raise ValueError(f"screened_rows must be a list of source-grounding objects in {path}")
        for index, row in enumerate(screened):
            node_id = _node_id(row, nodes)
            identifiers = row.get("grounded_external_ids", [])
            if not isinstance(identifiers, list) or any(not isinstance(value, str) for value in identifiers):
                raise ValueError(f"grounded_external_ids must be a list of identifiers in {path}")
            if identifiers:
                prior_grounding[node_id].append({
                    "standard_ids": identifiers,
                    "basis": row.get("screening"),
                    "artifact_record": {"path": source["path"], "json_pointer": f"/screened_rows/{index}"},
                })
        for group in ("rows", "confirmed", "secondary_edges"):
            rows = artifact.get(group, [])
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError(f"{group} must be a list of alignment objects in {path}")
            for index, row in enumerate(rows):
                node_id = _node_id(row, nodes)
                evidence_source = {"path": source["path"], "json_pointer": f"/{group}/{index}"}
                if group == "rows":
                    status = row.get("decision_status")
                    if status not in {"ALIGNED", "REVIEW_REQUIRED", "UNMATCHED"}:
                        raise ValueError(f"Unexpected alignment decision status in {path}: {status}")
                    assessments[node_id] = {**row, "artifact_record": evidence_source}
                    if status != "ALIGNED":
                        continue
                elif row.get("decision_status") not in (None, "ALIGNED"):
                    continue
                target = row.get("ontology_node_id")
                relation = row.get("relation_type")
                if (
                    not isinstance(target, str)
                    or not target.strip()
                    or not isinstance(relation, str)
                    or not relation.strip()
                    or relation == "UNMATCHED"
                    or not row.get("evidence")
                ):
                    raise ValueError(f"Supported connection lacks endpoint, relation or evidence in {path}")
                if (group == "rows") != (relation in STRICT_RELATIONS):
                    raise ValueError(f"Unexpected relation {relation} in {group} of {path}")
                key = node_id, target, relation
                edge = edges.setdefault(key, {**row, "artifact_records": []})
                edge["artifact_records"].append(evidence_source)
    if set(assessments) != set(nodes):
        raise ValueError("Alignment assessments must cover the supplied KG snapshot")
    strict_pairs = defaultdict(set)
    for node_id, target, relation in edges:
        if relation in STRICT_RELATIONS:
            strict_pairs[node_id].add((target, relation))
    if any(len(pairs) > 1 for pairs in strict_pairs.values()):
        raise ValueError("Conflicting strict alignment connections across baseline artifacts")
    return {
        "rows": list(edges.values()),
        "covered_ids": sorted({node_id for node_id, _, _ in edges}),
        "identity_ids": sorted({node_id for node_id, _, relation in edges if relation == IDENTITY}),
        "assessment_by_id": assessments,
        "prior_grounding_by_id": dict(prior_grounding),
    }, inputs


def _balanced_sample(nodes, covered_ids, size, seed):
    strata = defaultdict(list)
    for node in nodes:
        if node["id"] not in covered_ids:
            strata[node["kind"]].append(node)
    if type(size) is not int or not 1 <= size <= sum(map(len, strata.values())):
        raise ValueError("Sample size must be a positive integer within the uncovered population")
    if type(seed) is not int:
        raise ValueError("Sampling seed must be an integer")
    if size < len(strata):
        raise ValueError("Sample size must allow at least one node from each uncovered entity type")

    def rank(node):
        return hashlib.sha256(f"{seed}:{node['id']}".encode("utf-8")).digest(), node["id"]

    ordered = [iter(sorted(rows, key=rank)) for _, rows in sorted(strata.items())]
    sample = []
    while len(sample) < size:
        for group in ordered:
            node = next(group, None)
            if node is not None:
                sample.append(node)
                if len(sample) == size:
                    break
    return sample, {
        "seed": seed,
        "strategy": "balanced round-robin across entity types after sorting each type by SHA-256 of seed:Neo4j element ID",
        "eligible_by_type": {kind: len(rows) for kind, rows in sorted(strata.items())},
        "sample_by_type": dict(Counter(node["kind"] for node in sample)),
        "sample_size": len(sample),
    }


def _frozen_sample(path, nodes, baseline, snapshot_file, alignment_files):
    path = Path(path)
    manifest = read_json(path)
    if not isinstance(manifest, dict):
        raise ValueError("Invalid enrichment cohort manifest")
    signature = manifest.get("signature")
    signed = {key: value for key, value in manifest.items() if key != "signature"}
    if (signed.get("version") != "enrichment-frozen-cohort-v1"
            or signed.get("cohort") not in {"replay", "unseen"}
            or signature != digest(signed)):
        raise ValueError("Invalid or changed enrichment cohort manifest")
    if signed["kg_snapshot"] != snapshot_file or signed["alignment_files"] != alignment_files:
        raise ValueError("The enrichment cohort must use its frozen KG and alignment inputs")
    entries = signed.get("nodes")
    if not isinstance(entries, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(row.get("kind"), str)
        for row in entries
    ):
        raise ValueError("Cohort nodes must have string IDs and entity types")
    covered = set(baseline["covered_ids"])
    ids = [row["id"] for row in entries]
    if (not 1 <= len(ids) <= 200 or len(ids) != len(set(ids)) or covered.intersection(ids)
            or any(n["id"] not in nodes or nodes[n["id"]]["kind"] != n["kind"] for n in entries)):
        raise ValueError("The cohort must contain unique, uncovered nodes of the recorded type")
    return [nodes[ident] for ident in ids], {
        **signed["selection"], "sample_size": len(ids),
        "sample_by_type": dict(Counter(nodes[ident]["kind"] for ident in ids)),
        "cohort": signed["cohort"], "cohort_file": _file_reference(path),
        "cohort_signature": signature, "baseline_ontology": signed["baseline_ontology"],
    }


def load_enrichment_inputs(kg_snapshot_path, alignment_paths, *, sample_size=42, seed=20261122,
                           cohort_path=None):
    """Select only KG nodes with no supported baseline identity or category connection.

    The full snapshot and alignment inputs must refer to the same database extract.
    Category connections contribute to published coverage without counting as identity.
    """
    snapshot = load_kg_snapshot(kg_snapshot_path)
    nodes = {node["id"]: node for node in snapshot["nodes"]}
    baseline, alignment_inputs = _load_alignment(alignment_paths, nodes)
    source = snapshot["metadata"]["snapshot_file"]
    if cohort_path is not None:
        sample, design = _frozen_sample(cohort_path, nodes, baseline, source, alignment_inputs)
    else:
        sample, design = _balanced_sample(nodes.values(), set(baseline["covered_ids"]), sample_size, seed)
    positions = {node_id: index for index, node_id in enumerate(nodes)}
    sample = [
        {
            **node,
            "kg_record": {"path": source["path"], "json_pointer": f"/nodes/{positions[node['id']]}"},
            "baseline_assessment": baseline["assessment_by_id"][node["id"]],
            "prior_grounding": baseline["prior_grounding_by_id"].get(node["id"], []),
        }
        for node in sample
    ]
    return {
        "nodes": snapshot["nodes"],
        "sample": sample,
        "baseline": baseline,
        "metadata": {"kg_snapshot": snapshot["metadata"], "alignment_files": alignment_inputs, "sampling": design},
    }
