"""Portable, content-addressed v3 graph files for offline alignment."""

import hashlib
import json
from pathlib import Path

from .backbone_pathways import file_sha256
from .backbone_schema import SNAPSHOT_VERSION, V3_NODE_KEYS
from .storage import atomic_json, atomic_jsonl, read_json


DEFAULT_V3_DATA = Path(__file__).resolve().parents[2] / "data" / "backbone_v3"


def snapshot_id(files):
    digest = hashlib.sha256(SNAPSHOT_VERSION.encode())
    for name, facts in sorted(files.items()):
        digest.update(name.encode() + b"\0" + bytes.fromhex(facts["sha256"]))
    return digest.hexdigest()


def export_snapshot(plan, directory):
    directory = Path(directory)
    nodes = (
        {"label": label, "id": identifier, "key": V3_NODE_KEYS[label],
         "properties": dict(sorted(properties.items()))}
        for (label, identifier), properties in sorted(plan.nodes.items())
    )
    edges = (
        {"type": predicate, "id": identifier, "key": key, "start": edge.start, "end": edge.end,
         "properties": dict(sorted(edge.properties.items()))}
        for (predicate, key, identifier), edge in sorted(plan.relationships.items())
    )
    files = {}
    for name, rows, count in (("nodes", nodes, len(plan.nodes)),
                              ("relationships", edges, len(plan.relationships))):
        path = directory / f"{name}.jsonl"
        atomic_jsonl(path, rows)
        files[path.name] = {"rows": count, "sha256": file_sha256(path)}
    manifest = {"version": SNAPSHOT_VERSION, "snapshot_id": snapshot_id(files), "files": files}
    atomic_json(directory / "manifest.json", manifest)
    return manifest


def read_snapshot_manifest(directory):
    manifest = read_json(Path(directory) / "manifest.json")
    if manifest["version"] != SNAPSHOT_VERSION or set(manifest["files"]) != {"nodes.jsonl", "relationships.jsonl"}:
        raise ValueError("Unsupported v3 backbone snapshot.")
    if manifest["snapshot_id"] != snapshot_id(manifest["files"]):
        raise ValueError("The backbone snapshot ID differs from the file manifest.")
    return manifest


def read_snapshot_rows(directory, manifest, filename):
    path = Path(directory) / filename
    facts = manifest["files"][filename]
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != facts["sha256"]:
            raise ValueError(f"{filename} differs from the backbone snapshot manifest.")
        stream.seek(0)
        rows = [json.loads(line) for line in stream if line.strip()]
    if len(rows) != facts["rows"]:
        raise ValueError(f"{filename} does not have the expected row count.")
    return rows


def read_snapshot_plan(directory):
    """Validate the portable graph and reconstruct the same Neo4j import plan."""
    from .backbone import BackbonePlan
    from .backbone_schema import ROOT_CLASS_ID, V3_RELATION_KEYS

    manifest = read_snapshot_manifest(directory)
    plan = BackbonePlan({}, version="v3", node_keys=V3_NODE_KEYS, relationship_keys=V3_RELATION_KEYS)
    for row in read_snapshot_rows(directory, manifest, "nodes.jsonl"):
        label, key, identifier, props = row["label"], row["key"], row["id"], row["properties"]
        if V3_NODE_KEYS.get(label) != key or props.get(key) != identifier:
            raise ValueError(f"Unsupported snapshot node key on {label} {identifier}.")
        _check_provenance(props)
        plan.add_node(label, props)
    if ("BackboneClass", ROOT_CLASS_ID) not in plan.nodes:
        raise ValueError("The v3 snapshot is missing the LiverTrail Entity root.")
    for row in read_snapshot_rows(directory, manifest, "relationships.jsonl"):
        start, end = row["start"], row["end"]
        signature = (row["type"], start[0], end[0])
        props, key = row["properties"], row["key"]
        if V3_RELATION_KEYS.get(signature) != key or props.get(key) != row["id"]:
            raise ValueError(f"Unsupported snapshot relationship key on {row['type']} {row['id']}.")
        _check_provenance(props)
        if props.get("evidence_class") not in {"direct_disease_evidence", "contextual_background", "inferred_or_hypothesis"}:
            raise ValueError(f"Missing evidence class on snapshot relationship {row['id']}.")
        plan.add_relationship(signature, start[1], end[1], props)
    expected_nodes = manifest["files"]["nodes.jsonl"]["rows"]
    expected_edges = manifest["files"]["relationships.jsonl"]["rows"]
    if len(plan.nodes) != expected_nodes or len(plan.relationships) != expected_edges:
        raise ValueError("The v3 snapshot contains repeated graph keys.")
    return plan, manifest


def _check_provenance(properties):
    if properties.get("backbone_version") != "v3" or not properties.get("source") or not properties.get("source_version"):
        raise ValueError("A v3 snapshot row is missing source provenance.")
