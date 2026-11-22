"""Select, verify, stage, and re-align KG concepts in a separate local graph."""

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from graphlib import TopologicalSorter
from pathlib import Path
from threading import Lock

from ..alignment.candidates import canonical_standard_id
from ..alignment.loading import load_ontology
from ..alignment.pipeline import AlignmentModel
from ..hierarchy.entity_reference import EntityReferenceLookup
from ..hierarchy.ols_reference import OLSReferenceAgent
from ..hierarchy.ontology_ols import OntologyOLS
from ..hierarchy.ranking_journal import BudgetExceeded, ContractError
from ..model import ProviderError
from ..pipeline import file_hash
from ..storage import atomic_json, digest, read_json, run_lock
from .inputs import load_enrichment_inputs
from .obo import OBOCatalog
from .placement import load_dag, place_verified
from .sources import DefinitionSources
from .verification import realign, verify_concept

_VERSION = "kg-guided-enrichment-v1"


class _Models:
    def __init__(self, config, directory, maximum_calls):
        self.alignment = AlignmentModel(config, directory, maximum_calls)
        self.journal = self.alignment.journal
        self.parameters = self.alignment.settings["parameters"]
        self.lock = Lock()

    def request(self, task, refs):
        stage, prompt, payload = task
        key = stage if stage == "ols_reference_action" else f"{stage}-{digest(prompt)[:8]}"
        with self.lock:
            self.journal.prompts.setdefault(key, prompt)
            self.journal.settings.setdefault(key, self.parameters)
        self.journal.last_ref = None
        try:
            response = self.journal.call(key, payload)
            candidate_review = stage in {"enrichment_parent_fallback_review", "enrichment_typed_relation_review"}
            if isinstance(response, list):
                rows = response
            elif isinstance(response, dict) and "rows" in response:
                rows = response["rows"]
            elif candidate_review and isinstance(response, dict) and "items" in response:
                rows = response["items"]
            elif isinstance(response, dict) and "id" in response and (candidate_review or len(payload["items"]) == 1):
                rows = [response]
            else:
                raise ContractError("Expected model response envelope rows")
            expected = Counter(item["id"] for item in payload["items"])
            if candidate_review and isinstance(rows, list):
                # These stages check each candidate and retry only missing or invalid judgments.
                return rows
            if not isinstance(rows, list) or any(not isinstance(row, dict) or
                    not isinstance(row.get("id"), str) for row in rows):
                raise ContractError("Expected model response rows with IDs")
            received = Counter(row["id"] for row in rows)
            if received != expected:
                # A proposal shard asks about one target. Other identified targets
                # have no effect on it. Its candidate judgments are still checked by placement.
                if stage == "incremental_propose" and len(payload["items"]) == 1 and len(received) == len(rows):
                    target_id = payload["items"][0]["id"]
                    target = next((row for row in rows if row["id"] == target_id), None)
                    if (target is not None and isinstance(target.get("comparisons"), list)
                            and "new_parent" in target and all(row["id"].strip() for row in rows)):
                        return [target]
                raise ContractError("Response must cover each requested ID exactly once")
            return rows
        finally:
            if self.journal.last_ref:
                with self.lock:
                    refs.append(self.journal.last_ref)


def _alignment_nodes(dag, groundings):
    parents = defaultdict(list)
    for child, parent in sorted(dag.edges):
        parents[child].append(parent)
    return [{
        **node, "aliases": node.get("aliases", []), "parent_ids": parents[ident],
        "is_root": ident in dag.roots.values(),
        "standard_ids": groundings.get(ident, node.get("standard_ids", [])),
    } for ident, node in dag.nodes.items()]


def _concept(kg):
    source = kg["source"]
    definition_evidence = kg["definition_evidence"]
    sourced = {key: kg[key] for key in ("reference_terms", "auxiliary_evidence") if kg.get(key)}
    return {
        "id": "KG-" + digest([source["database"], source["element_id"]])[:20],
        "kind": kg["kind"], "name": kg["name"], "definition": kg["definition"],
        "aliases": kg["aliases"], "standard_ids": kg["standard_ids"],
        "species": kg.get("species"), "taxon_id": kg.get("taxon_id"),
        "entity_level": kg.get("entity_level"),
        "entity_level_evidence": kg.get("entity_level_evidence", []),
        "definition_evidence": definition_evidence,
        "source_context": kg.get("source_context", {}),
        "source_mentions": [], "origin": "kg_enrichment", **sourced,
        "enrichment_provenance": {
            "kg_source": source, "kg_record": kg.get("kg_record"),
            "definition_evidence": definition_evidence,
            "scope": "Same KG record, name, type, taxon, and definition checked before placement.",
            **sourced,
        },
    }


def _mapping(kg, target, outcome, record_ref):
    return {
        "kg_node_id": kg["id"], "kg_name": kg["name"], "entity_type": kg["kind"],
        "ontology_node_id": target["id"], "ontology_name": target["name"],
        "relation_type": "MAPS_TO", "decision_status": "ALIGNED",
        "evidence": {"record": record_ref, "kg_source": kg["source"],
                     "identity": outcome["identity_evidence"],
                     "alignment_decision": outcome["alignment"]["evidence"]},
    }


def _case(dag, kg, source_audit, output, models, alignment, groundings, *, use_ols,
          catalog=None, parent_lookup=None):
    case_id = digest(kg["id"])[:20]
    location = output / "alignment" / case_id
    record_ref = f"records/{case_id}.json"
    refs = []
    record = {"kg": kg, "source_audit": source_audit, "status": "needs_review",
              "decision_stage": "source_verification",
              "verification": None, "placement": None, "realignment": None,
              "mapping": None, "model_call_refs": refs, "record_ref": record_ref}
    prior = kg.get("baseline_assessment") or {}
    if prior.get("outcome") == "INPUT_ISSUE":
        record.update(status="rejected", reason="prior_source_review_identified_input_issue")
        return dag, record
    if (kg.get("definition_evidence") or {}).get("scope_review_required"):
        record["reason"] = "definition_scope_requires_review"
        return dag, record
    node_view = _alignment_nodes(dag, groundings)
    if kg.get("definition_evidence") and (record_id := kg["definition_evidence"].get("record_id")):
        # Only a source-confirmed, supplied record ID may become a strong identity key.
        grounded = {ident for row in kg.get("prior_grounding", []) for ident in row["standard_ids"]}
        if record_id in grounded:
            kg["standard_ids"] = sorted({*kg["standard_ids"], record_id})
    try:
        def request(task):
            return models.request(task, refs)

        record["decision_stage"] = "identity_verification"
        before = location / "before" / digest([kg, node_view])[:16]
        verification = verify_concept(kg, node_view, output=before, model=alignment, scope_request=request)
        record["verification"] = {**verification, "trace": str(before.relative_to(output))}
        if verification["action"] == "existing":
            target = dag.nodes[verification["ontology_node_id"]]
            record.update(status="mapped_existing", decision_stage="mapped", reason=verification["reason"],
                          mapping=_mapping(kg, target, verification, record_ref))
            return dag, record
        if verification["action"] != "new":
            record["reason"] = verification["reason"]
            return dag, record
        if not kg.get("definition_evidence"):
            record["decision_stage"] = "source_verification"
            record["reason"] = "source_definition_missing"
            return dag, record
        if prior.get("outcome") == "UNRESOLVED" and not kg.get("standard_ids"):
            record["decision_stage"] = "source_verification"
            record["reason"] = "previous_source_scope_ambiguity_unresolved"
            return dag, record

        concept = _concept(kg)

        record["decision_stage"] = "hierarchy_placement"
        fallback_ready = parent_lookup is not None
        lookup = EntityReferenceLookup(
            output / "ols", ols=OLSReferenceAgent(request, catalog=catalog, parent_fallback=fallback_ready),
            settings={**alignment.settings, "ols_parent_fallback": fallback_ready},
        ) if use_ols else None
        staged, placement = place_verified(dag, concept, request=request, entity_lookup=lookup,
                                            known_standard_ids=groundings, parent_lookup=parent_lookup,
                                            typed_review=True)
        record["placement"] = placement
        if placement["status"] != "inserted":
            record.update(status="rejected" if placement["status"] == "rejected" else "needs_review",
                          reason=placement["reason"])
            return dag, record
        record["decision_stage"] = "identity_realignment"
        staged_view = _alignment_nodes(staged, groundings)
        after = location / "after" / digest([kg, staged_view])[:16]
        recheck = realign(kg, staged_view, concept["id"], output=after,
                         model=alignment, require_source_anchor=True,
                         placement_evidence=placement["edge_evidence"])
        record["realignment"] = {**recheck, "trace": str(after.relative_to(output))}
        if not recheck["accepted"]:
            record["reason"] = recheck["reason"]
            return dag, record
        record.update(status="inserted", decision_stage="mapped", reason=placement["reason"],
                      mapping=_mapping(kg, staged.nodes[concept["id"]], recheck, record_ref))
        return staged, record
    except (ProviderError, BudgetExceeded, ContractError) as error:
        record.update(reason="model_service_budget_or_contract_error", error=str(error))
        return dag, record


def _export(ontology, dag, inputs, records, output, manifest, groundings):
    mappings, new_edges = [], []
    for record in records:
        if record["mapping"]:
            mappings.append(record["mapping"])
        if record["status"] == "inserted":
            for edge in record["placement"]["edge_evidence"]:
                new_edges.append({**edge, "relation": "is_a", "evidence_record": record["record_ref"]})
    if len(new_edges) != len({(e["child_id"], e["parent_id"]) for e in new_edges}):
        raise ValueError("Multiple insertion records claim the same new edge")
    asserted = [*ontology["edges"], *new_edges]
    if dag.edges != {(e["child_id"], e["parent_id"]) for e in asserted}:
        raise ValueError("Every added edge must have a corresponding evidence record")

    original = {node["id"]: node for node in ontology["nodes"]}
    new_ids = set(dag.nodes) - set(original)
    by_standard_id = defaultdict(list)
    for node in _alignment_nodes(dag, groundings):
        for raw_id in node["standard_ids"]:
            if ident := canonical_standard_id(raw_id):
                by_standard_id[ident].append((node["id"], node["kind"]))
    for ident, owners in by_standard_id.items():
        if len({kind for _, kind in owners}) > 1 and any(node in new_ids for node, _ in owners):
            raise ValueError(f"New concept introduced a cross-type standard ID collision: {ident}")
    for node in dag.nodes.values():
        if node["id"] in new_ids and not node.get("definition_evidence"):
            raise ValueError("Every added concept must keep its definition evidence")
    by_id = {row["kg_node_id"]: row for row in mappings}
    if len(by_id) != len(mappings) or any(row["ontology_node_id"] not in dag.nodes for row in mappings):
        raise ValueError("Mapping endpoints must be unique KG records pointing to placed concepts")
    baseline = inputs["baseline"]
    total = len(inputs["nodes"])
    before = set(baseline["covered_ids"])
    identity = set(baseline["identity_ids"])
    if before & set(by_id):
        raise ValueError("An enriched KG node was already covered by the baseline")
    after = before | set(by_id)
    identity_after = identity | set(by_id)
    outcomes = Counter(record["status"] for record in records)
    summary = {
        "sample_size": len(records), "outcomes": dict(outcomes),
        "by_type": {kind: dict(Counter(r["status"] for r in records if r["kg"]["kind"] == kind))
                    for kind in sorted(dag.roots)},
        "review_and_rejection_reasons": dict(Counter(r["reason"] for r in records
                                             if r["status"] in {"needs_review", "rejected"})),
        "new_kg_concepts": outcomes["inserted"],
        "new_intermediate_parents": len(new_ids) - outcomes["inserted"],
        "ontology_nodes_before": len(original), "ontology_nodes_after": len(dag.nodes),
        "is_a_before": len(ontology["edges"]), "is_a_after": len(asserted),
        "new_edge_support": dict(Counter(edge["support"] for edge in new_edges)),
        "new_maps_to": len(mappings),
        "nodes_with_placement_ols": sum(bool((r["placement"] or {}).get("reference_audits")) for r in records),
        "placement_ols_audits": sum(len((r["placement"] or {}).get("reference_audits", [])) for r in records),
        "nodes_with_ols_definition_lookup": sum(any(o.get("source") in {"MONDO", "CL", "Uberon", "ChEBI"}
                                                   for o in r["source_audit"]["observations"]) for r in records),
        "coverage": {
            "kg_population": total, "baseline_supported_kg_nodes": len(before),
            "expanded_supported_kg_nodes": len(after), "supported_before": len(before) / total,
            "supported_after": len(after) / total, "baseline_maps_to_kg_nodes": len(identity),
            "expanded_maps_to_kg_nodes": len(identity_after),
            "maps_to_before": len(identity) / total, "maps_to_after": len(identity_after) / total,
            "sample_maps_to_before": 0, "sample_maps_to_after": len(mappings) / len(records),
            "method": "Carry forward frozen baseline edges and add verified pilot MAPS_TO; no full-KG re-alignment.",
        },
    }
    expanded = {
        "method": _VERSION, "status": "complete", "edges_are_provisional": ontology.get("edges_are_provisional"),
        "roots": dag.roots, "nodes": list(dag.nodes.values()), "edges": asserted,
        "enrichment": {"baseline_ontology": manifest["ontology"], "new_node_ids": sorted(new_ids),
                       "new_edges": new_edges, "maps_to_file": "alignment-enriched.json"},
    }
    for ident, original_node in original.items():
        if dag.nodes[ident] != original_node:
            raise ValueError(f"Original ontology node changed in the enrichment snapshot: {ident}")
    TopologicalSorter({node["id"]: node["parent_ids"] for node in _alignment_nodes(dag, {})}).prepare()
    atomic_json(output / "ontology-enriched.json", expanded)
    atomic_json(output / "alignment-enriched.json", {"baseline_edges": baseline["rows"], "new_maps_to": mappings})
    atomic_json(output / "records.json", records)
    atomic_json(output / "summary.json", summary)
    return summary


def _workflow_files():
    package = Path(__file__).resolve().parent.parent
    modules = [*package.glob("storage.py"), *package.glob("model.py"), *package.glob("entity_types.py")]
    for namespace in ("alignment", "enrichment", "hierarchy"):
        modules.extend((package / namespace).glob("*.py"))
    modules.extend((package / "alignment" / "prompts").glob("*.txt"))
    return {str(path.relative_to(package)): file_hash(path) for path in sorted(modules)
            if path.name != "experiment.py"}


def run(*, ontology_path, kg_snapshot, alignment_paths, output, config, sample_size=42,
        seed=20261122, maximum_calls=420, articles=(), use_ols=True, workers=5,
        ontology_grounding=None, obo_paths=None, progress=None, cohort_path=None):
    output = Path(output).resolve()
    obo_paths = obo_paths or {}
    input_paths = [ontology_path, kg_snapshot, *alignment_paths, config, *articles, *obo_paths.values()]
    if ontology_grounding:
        input_paths.append(ontology_grounding)
    if cohort_path:
        input_paths.append(cohort_path)
    if any(Path(path).resolve().is_relative_to(output) for path in input_paths):
        raise ValueError("Keep inputs outside the enrichment output directory")
    if (cohort_path is None and not 30 <= sample_size <= 50) or not 1 <= workers <= 7 or maximum_calls < 1:
        raise ValueError("Use 30–50 sample nodes, 1–7 workers, and a positive model call budget")
    inputs = load_enrichment_inputs(kg_snapshot, alignment_paths, sample_size=sample_size, seed=seed,
                                    cohort_path=cohort_path)
    ontology_ref = {"path": str(Path(ontology_path).resolve()), "sha256": file_hash(Path(ontology_path))}
    if cohort_path and ontology_ref != inputs["metadata"]["sampling"]["baseline_ontology"]:
        raise ValueError("The enrichment cohort must use its frozen ontology")
    ontology = read_json(ontology_path)
    baseline_dag = load_dag(ontology)
    original_ids = set(baseline_dag.nodes)
    if any(row["ontology_node_id"] not in original_ids for row in inputs["baseline"]["rows"]):
        raise ValueError("The baseline alignment refers to concepts absent from the supplied ontology")
    groundings = {n["id"]: n["standard_ids"] for n in load_ontology(ontology_path)["nodes"]}
    if ontology_grounding:
        supplement = read_json(ontology_grounding)["nodes"]
        if {n["id"]: (n["kind"], n["name"], n["definition"]) for n in supplement} != {
            n["id"]: (n["kind"], n["name"], n["definition"]) for n in ontology["nodes"]
        }:
            raise ValueError("Grounding supplement does not match the original ontology concepts")
        for n in supplement:
            groundings[n["id"]] = sorted({*groundings[n["id"]], *n.get("standard_ids", [])})
    obo = OBOCatalog(obo_paths) if obo_paths else None
    catalog = OntologyOLS()

    def lookup_parent(source, term_id):
        if obo is not None:
            local = obo.inspect_term(source, term_id)
            if local["record"] is not None:
                return local
        return catalog.inspect_term(source, term_id) if use_ols else {"record": None}

    manifest = {
        "obo_sources": obo.metadata if obo else {}, "version": _VERSION, "inputs": inputs["metadata"],
        "ontology": ontology_ref,
        "supplemental_ontology_grounding_sha256": file_hash(Path(ontology_grounding)) if ontology_grounding else None,
        "config_sha256": file_hash(Path(config)), "maximum_llm_calls": maximum_calls,
        "articles": [{"path": str(Path(path).resolve()), "sha256": file_hash(Path(path))} for path in articles],
        "ols_enabled": use_ols, "writes_to_neo4j": False,
    }
    if cohort_path:
        manifest.update(workers=workers, workflow_files=_workflow_files())
    output.mkdir(parents=True, exist_ok=True)
    with run_lock(output):
        path = output / "manifest.json"
        models = _Models(config, output, maximum_calls)
        manifest["model"] = models.alignment.settings
        if path.exists() and read_json(path)["signature"] != digest(manifest):
            raise ValueError("Output belongs to a different experiment")
        signature = digest(manifest)
        if path.exists() and read_json(path)["status"] == "complete":
            return read_json(output / "summary.json")
        atomic_json(path, {**manifest, "signature": signature, "status": "running"})
        atomic_json(output / "sample.json", {"metadata": inputs["metadata"], "nodes": inputs["sample"]})
        sources = DefinitionSources(output / "sources", articles=articles, allow_ols=use_ols, obo=obo,
                                    catalog=catalog)
        by_type = defaultdict(list)
        for node in inputs["sample"]:
            by_type[node["kind"]].append(node)

        def enrich_type(group):
            # Preserve order within type, since each placement sees earlier concepts
            # of that type. Every worker still sees the same cross-type baseline.
            dag = copy(baseline_dag)
            alignment = copy(models.alignment)
            records = []
            for node in group:
                kg, audit = sources.resolve(node)
                dag, record = _case(dag, kg, audit, output, models, alignment, groundings, use_ols=use_ols,
                                    catalog=catalog, parent_lookup=lookup_parent if obo or use_ols else None)
                atomic_json(output / record["record_ref"], record)
                records.append(record)
                if progress:
                    progress(record)
            return dag, records

        completed = {}
        enriched = copy(baseline_dag)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for dag, records in pool.map(enrich_type, by_type.values()):
                enriched.nodes = {**enriched.nodes, **{k: v for k, v in dag.nodes.items() if k not in original_ids}}
                enriched.edges = enriched.edges | dag.edges
                completed.update((record["kg"]["id"], record) for record in records)
        ordered = [completed[node["id"]] for node in inputs["sample"]]
        if cohort_path and manifest["workflow_files"] != _workflow_files():
            raise ValueError("The frozen evidence workflow changed during the run")
        summary = _export(ontology, enriched, inputs, ordered, output, manifest, groundings)
        atomic_json(path, {**manifest, "signature": signature, "status": "complete"})
        return summary
