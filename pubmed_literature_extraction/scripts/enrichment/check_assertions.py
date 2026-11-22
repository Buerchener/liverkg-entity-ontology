"""Independently check new evidence paths against raw named OWL superclass assertions.

Use a completed enrichment run and its frozen evidence. No Neo4j connection.
"""
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import argparse
import hashlib
import json
import os
from graphlib import TopologicalSorter
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote
from urllib.request import Request, urlopen


SUBCLASS = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
SLUG = {"PR": "pr", "CHEBI": "chebi", "HGNC": "hgnc", "SO": "so", "CL": "cl",
        "UBERON": "uberon", "MONDO": "mondo", "GO": "go"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def iri(ident):
    prefix, local = ident.split(":", 1)
    if prefix == "HGNC":
        return "https://www.genenames.org/data/gene-symbol-report/#!/hgnc_id/" + local
    return "http://purl.obolibrary.org/obo/" + prefix + "_" + local


@lru_cache(maxsize=128)
def official_record(ident, archive):
    slug = SLUG[ident.split(":")[0]]
    url = "https://www.ebi.ac.uk/ols4/api/v2/ontologies/" + slug + "/classes/" + quote(quote(iri(ident), safe=""), safe="")
    request = Request(url, headers={"User-Agent": "LiverKG-Enrichment-Audit/1.0", "Accept": "application/json"})
    archive.mkdir(parents=True, exist_ok=True)
    body = None
    for folder in [archive, archive.parent.parent/"evidence-v3"/"audit-source-records", archive.parent.parent/"evidence-v2"/"audit-source-records"]:
        for path in folder.glob("*.json"):
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() == path.stem and json.loads(content).get("iri") == iri(ident):
                body = content
                break
        if body:
            break
    if body is None:
        with urlopen(request, timeout=14) as response:
            body = response.read()
    data = json.loads(body)
    if data["iri"] != iri(ident) or data["isObsolete"] is not False or "class" not in data["type"]:
        raise ValueError("Official endpoint is not the requested active class.")
    checksum = hashlib.sha256(body).hexdigest()
    # Preserve the exact response bytes for hash verification. A unique temporary
    # file makes concurrent audit reads safe, including during independent runs.
    with NamedTemporaryFile(dir=archive, suffix=".tmp", delete=False) as tmp:
        tmp.write(body)
    os.replace(tmp.name, archive/(checksum + ".json"))
    return url, checksum, data


def frozen_file(ref, inputs_dir):
    """Find an input by its recorded path or by matching its name and checksum after relocation."""
    recorded = Path(ref["path"])
    if inputs_dir is not None:
        for path in inputs_dir.rglob(recorded.name):
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == ref["sha256"]:
                return path
    return recorded


@lru_cache(maxsize=8)
def obo_lines(path, expected_hash, inputs_dir):
    body = frozen_file({"path": path, "sha256": expected_hash}, inputs_dir).read_bytes()
    if hashlib.sha256(body).hexdigest() != expected_hash:
        raise ValueError("OBO digest differs from recorded source")
    return body.decode().splitlines()


def exact_obo_assertion(hop, inputs_dir):
    artifact = hop.get("artifact") or {}
    number = hop.get("line")
    if not artifact.get("path") or not artifact.get("sha256") or not isinstance(number, int):
        return None
    lines = obo_lines(artifact["path"], artifact["sha256"], inputs_dir)
    if not 1 <= number <= len(lines):
        return False
    entry = lines[number - 1].split("!", 1)[0].strip()
    if entry != "is_a: " + hop["parent_id"]:
        return False
    for idx in range(number - 2, -1, -1):
        line = lines[idx].strip()
        if line.startswith("id: "):
            return line[4:].strip() == hop["child_id"]
        if line.startswith("["):
            break
    return False


def named_iris(raw):
    """Only named URI-valued class assertions, optionally annotated by OWL reification."""
    values = raw if isinstance(raw, list) else [raw]
    for value in values:
        if isinstance(value, dict):
            types = value.get("type", [])
            types = [types] if isinstance(types, str) else types
            if not isinstance(types, list) or "reification" not in types or "restriction" in types:
                continue
            value = value.get("value")
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            yield value


def check_hop(hop, archive, inputs_dir):
    child, parent = hop["child_id"], hop["parent_id"]
    check = {"child": child, "parent": parent}
    obo = exact_obo_assertion(hop, inputs_dir)
    if obo is not None:
        check.update(named_obo_subclass=obo, obo_artifact=hop["artifact"], obo_line=hop["line"])
        if obo:
            return check
    try:
        url, checksum, data = official_record(child, archive)
        values = list(named_iris(data.get(SUBCLASS)))
        check.update(url=url, response_sha256=checksum, named_owl_subclass=iri(parent) in values,
                     ols_direct_class_parent=iri(parent) in named_iris(data.get("directParent")))
    except (ValueError, KeyError, OSError) as error:
        check["lookup_error"] = str(error)
    return check


def independently_find_path(child, parent, archive):
    # This is an evaluation-only read of raw OWL after output has already been committed.
    # Start only at an identity previously confirmed by the source, never at a name-search hit.
    starts = [t["term_id"] for t in child.get("reference_terms", []) if t.get("identity_evidence")]
    ends = set(parent.get("standard_ids", []))
    allowed = {i.split(":")[0] for i in [*starts, *ends]} & SLUG.keys()
    frontier = [(s, []) for s in starts if s.split(":")[0] in allowed]
    visited = set()
    while frontier and len(visited) < 12:
        ident, path = frontier.pop(0)
        if ident in visited or len(path) == 3:
            continue
        visited.add(ident)
        try:
            _, _, data = official_record(ident, archive)
        except (ValueError, KeyError, OSError):
            continue
        for raw in named_iris(data.get(SUBCLASS)):
            if not isinstance(raw, str) or not raw.startswith("http://purl.obolibrary.org/obo/"):
                continue
            next_id = raw.rsplit("/", 1)[-1].replace("_", ":", 1)
            if next_id.split(":")[0] not in allowed:
                continue
            route = [*path, {"child_id": ident, "parent_id": next_id, "predicate": "is_a"}]
            if next_id in ends:
                return route
            frontier.append((next_id, route))
    return []


def check_edge(edge, nodes, root_ids, archive, inputs_dir):
    child, parent = nodes[edge["child_id"]], nodes[edge["parent_id"]]
    ref = edge.get("reference") or {}
    hops = ref.get("is_a_path") or ref.get("is_a_evidence") or []
    root = parent["id"] in root_ids
    result = {"child": child["name"], "child_id": child["id"], "parent": parent["name"], "parent_id": parent["id"],
              "schema_root": root, "support": edge["support"], "evidence_record": edge["evidence_record"],
              "same_local_kind": child["kind"] == parent["kind"],
              "child_definition": child["definition"], "parent_definition": parent["definition"],
              "child_ids": child.get("standard_ids", []), "parent_ids": parent.get("standard_ids", []),
              "child_reference_ids": [t["term_id"] for t in child.get("reference_terms", []) if t.get("identity_evidence")]}
    if root:
        result["child_kind"] = child["kind"]
        result["definition_evidence"] = child.get("definition_evidence")
        result["schema_membership_evidence"] = edge.get("schema_membership") or edge.get("schema_scope") or edge.get("reference")
        result["relation_evidence"] = {k: v for k, v in edge.items()
                                       if k not in {"child_id", "parent_id", "review", "evidence_record", "reference"}}
    if not root:
        if not hops:
            independently_found = independently_find_path(child, parent, archive)
            if independently_found:
                result["additional_raw_owl_proof"] = [check_hop(h, archive, inputs_dir) for h in independently_found]
        result["hops"] = [check_hop(h, archive, inputs_dir) for h in hops]
        start = (ref.get("start") or ref.get("child_term") or {}).get("term_id")
        end = (ref.get("parent_term") or {}).get("term_id")
        result["asserted_external_endpoints"] = [start, end]
        result["contiguous_child_to_parent"] = bool(
            hops and start == hops[0]["child_id"] and end == hops[-1]["parent_id"]
            and all(h["parent_id"] == k["child_id"] for h, k in zip(hops, hops[1:]))
            and all(h.get("predicate") == "is_a" for h in hops))
        evidence = result["hops"] or result.get("additional_raw_owl_proof", [])
        if evidence:
            src, dst = evidence[0]["child"], evidence[-1]["parent"]
            result["independent_path_pass"] = (
                all(h.get("named_owl_subclass") is True or h.get("named_obo_subclass") is True for h in evidence)
                and all(h["parent"] == k["child"] for h, k in zip(evidence, evidence[1:]))
                and (not hops or result["contiguous_child_to_parent"]))
            result["source_and_local_ids_agree"] = (
                src in set(result["child_reference_ids"] + result["child_ids"])
                and dst in result["parent_ids"])
    return result


def audit(run, archive, destination, inputs_dir):
    manifest = read(run/"manifest.json")
    assert manifest["status"] == "complete" and manifest["writes_to_neo4j"] is False
    graph = read(run/"ontology-enriched.json")
    base = read(frozen_file(manifest["ontology"], inputs_dir))
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert all(n == nodes[n["id"]] for n in base["nodes"])
    assert all(e == f for e, f in zip(base["edges"], graph["edges"])) and len(base["edges"]) <= len(graph["edges"])
    for frozen in [manifest["ontology"], manifest["inputs"]["kg_snapshot"]["snapshot_file"],
                   *manifest["inputs"]["alignment_files"]]:
        assert hashlib.sha256(frozen_file(frozen, inputs_dir).read_bytes()).hexdigest() == frozen["sha256"]
    assert len(nodes) == len(graph["nodes"])
    original_ids = {n["id"] for n in base["nodes"]}
    assert set(nodes) - original_ids == set(graph["enrichment"]["new_node_ids"])
    assert graph["edges"][len(base["edges"]):] == graph["enrichment"]["new_edges"]
    assert graph["roots"] == base["roots"]
    roots = set(graph["roots"].values())
    topology = TopologicalSorter()
    for e in graph["edges"]:
        assert e["child_id"] in nodes and e["parent_id"] in nodes
        topology.add(e["child_id"], e["parent_id"])
    topology.prepare()
    maps = read(run/"alignment-enriched.json")["new_maps_to"]
    records = {r["kg"]["id"]: r for r in read(run/"records.json")}
    assert len(maps) == len({m["kg_node_id"] for m in maps})
    assert {m["kg_node_id"] for m in maps} == {k for k, r in records.items() if r.get("mapping")}
    mapping_basis = {}
    for m in maps:
        r = records[m["kg_node_id"]]
        assert m["relation_type"] == "MAPS_TO" and m["decision_status"] == "ALIGNED"
        assert r["mapping"] == m and m["ontology_node_id"] in nodes
        if r["status"] == "inserted":
            assert nodes[m["ontology_node_id"]]["enrichment_provenance"]["kg_source"] == r["kg"]["source"]
            recheck = r["realignment"]
            assert recheck["accepted"]
            if recheck["alignment"]["decision_status"] == "ALIGNED":
                assert recheck["alignment"]["ontology_node_id"] == m["ontology_node_id"]
                assert recheck["alignment"]["relation_type"] == "MAPS_TO"
                mapping_basis[m["kg_node_id"]] = "raw_alignment"
            else:
                proof = recheck["identity_evidence"]
                assert recheck["reason"] == "preserved_kg_record_identity"
                assert proof["source_anchor"]["element_id"] == m["kg_node_id"]
                assert proof["source_anchor"]["database"] == r["kg"]["source"]["database"]
                assert not proof["unresolved_exact_matches"]
                mapping_basis[m["kg_node_id"]] = "scoped_kg_record_anchor"
        else:
            v = r["verification"]
            assert r["status"] == "mapped_existing" and v["action"] == "existing"
            assert v["ontology_node_id"] == m["ontology_node_id"]
            assert v["alignment"]["decision_status"] == "ALIGNED" and v["alignment"]["relation_type"] == "MAPS_TO"
            assert any(s["relation"] == "EQUIVALENT" and s["ontology_node_id"] == m["ontology_node_id"]
                       for s in v["identity_evidence"]["scope_resolutions"])
            mapping_basis[m["kg_node_id"]] = "existing_verified_identity"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda e: check_edge(e, nodes, roots, archive, inputs_dir), graph["enrichment"]["new_edges"]))
    for row in results:
        assert (run/row["evidence_record"]).is_file()
    specific = [r for r in results if not r["schema_root"]]
    confirmed = [r for r in specific if r.get("independent_path_pass") and r.get("source_and_local_ids_agree")]
    output = {"cohort": run.name, "specific_edges": len(specific), "specific_edges_formally_verified": len(confirmed),
              "specific_edges_needing_scope_or_assertion_review": [r for r in specific if r not in confirmed],
              "schema_edges_separate": len(results) - len(specific), "inputs_and_original_graph_verified": True,
              "acyclic_and_mapping_endpoints_checked": True, "maps_checked": len(maps),
              "mapping_evidence_basis": mapping_basis, "edges": results}
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if destination.exists():
        if destination.read_text(encoding="utf-8") != document:
            raise ValueError(f"Existing output differs; choose a distinct --output path: {destination}")
    else:
        destination.write_text(document, encoding="utf-8")
    print(run.name, len(results), "new assertions checked")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Completed run directory")
    parser.add_argument("--cache", type=Path, help="Directory of SHA-256-addressed OLS JSON records")
    parser.add_argument("--output", type=Path, help="Audit output JSON (default: RUN/assertion-audit.json)")
    parser.add_argument("--inputs-dir", type=Path, help="Look for relocated original input files here by name and checksum")
    args = parser.parse_args()
    run = args.run.resolve()
    audit(run, args.cache or run.parent/"audit-source-records", args.output or run/"assertion-audit.json", args.inputs_dir)
