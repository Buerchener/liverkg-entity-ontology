"""Standalone alignment orchestration; no graph mutations or ontology expansion."""

from collections import Counter
from datetime import datetime, timezone
import csv
import json
from pathlib import Path

from ..hierarchy.entity_reference import EntityReferenceLookup
from ..hierarchy.ols_reference import OLSReferenceAgent, POLICY as OLS_POLICY
from ..hierarchy.ranking_journal import LLMJournal, BudgetExceeded, ContractError
from ..model import ProviderError, endpoint, live_provider, read_settings
from ..storage import atomic_json, digest, read_json, run_lock
from .candidates import CandidateRetriever
from .decisions import POLICY, _valid_citations, admit, choose, reference_pair, validate_assessments
from .prompts import DEFAULT_PROMPT, PROMPT_VARIANTS


VERSION = "alignment-mvp-v1"


class AlignmentModel:
    def __init__(self, config_path, directory, maximum_calls, *, prompt_name=None):
        self.prompt_name = DEFAULT_PROMPT if prompt_name is None else prompt_name
        if not isinstance(self.prompt_name, str) or self.prompt_name not in PROMPT_VARIANTS:
            raise ValueError("Unknown alignment prompt: " + str(self.prompt_name))
        self.policy = PROMPT_VARIANTS[self.prompt_name]
        connection, parameters = read_settings(config_path, "audit_edges")
        self.settings = {"parameters": parameters, "endpoint": endpoint(connection),
                         "alignment_prompt": self.prompt_name,
                         "alignment_prompt_sha256": digest(self.policy)}
        provider = live_provider(connection, parameters)
        self.journal = LLMJournal(Path(directory) / "llm", provider,
                                  {"alignment": self.policy, "ols_reference_action": OLS_POLICY},
                                  {"alignment": parameters, "ols_reference_action": parameters}, maximum_calls)

    def assess(self, payload):
        self.last_call_refs = []
        original = payload
        previous_error = self.journal.invalid_reason("alignment", payload)
        if previous_error:
            payload = self._correction(original, previous_error)
        for attempt in range(2 if not previous_error else 1):
            self.journal.last_ref = None
            try:
                try:
                    response = self.journal.call("alignment", payload)
                finally:
                    if self.journal.last_ref:
                        self.last_call_refs.append(self.journal.last_ref)
                rows = validate_assessments(response, [r["id"] for r in payload["items"]])
                supplied = {r["id"]: r for r in payload["items"]}
                for row in rows:
                    _, invalid = _valid_citations(row, payload["kg"], supplied[row["id"]])
                    if invalid:
                        raise ContractError("Citation not found in its specified LOCAL input field")
                return rows
            except ContractError as exc:
                if self.journal.last_ref:
                    self.journal.mark_invalid(str(exc))
                if previous_error or attempt == 1:
                    raise
                payload = self._correction(original, str(exc))

    @staticmethod
    def _correction(payload, reason):
        return {**payload, "output_format_correction": reason +
                ". Return every candidate once with the specified JSON fields. Evidence quotes must "
                "be exact substrings of the LOCAL kg/ontology fields allowed in the system prompt. "
                "Do not relabel OLS/external text as local evidence. External evidence is retained "
                "separately by the program; discuss it in rationale. Remove unsupported citations. "
                "If the remaining evidence is insufficient, use UNCERTAIN; do not invent evidence."}

    def reference_request(self, task):
        stage, _, payload = task
        response = self.journal.call(stage, payload)
        if not isinstance(response, dict) or not isinstance(response.get("rows"), list):
            raise ContractError("Invalid OLS action envelope")
        return response["rows"]


def _compact(node):
    return {key: node.get(key) for key in (
        "id", "name", "kind", "definition", "aliases", "standard_ids", "parent_ids",
        "is_root", "entity_level", "entity_level_evidence", "taxon_id", "species",
        "source_context", "source_mentions")}


def summarize(rows):
    statuses = Counter(r["decision_status"] for r in rows)
    relations = Counter(r["relation_type"] for r in rows if r["decision_status"] == "ALIGNED")
    by_type = {}
    for kind in sorted({r["entity_type"] for r in rows}):
        group = [r for r in rows if r["entity_type"] == kind]
        aligned = sum(r["decision_status"] == "ALIGNED" for r in group)
        by_type[kind] = {"total": len(group), "aligned": aligned,
                         "coverage": aligned / len(group),
                         **{s: sum(r["decision_status"] == s for r in group)
                            for s in ("UNMATCHED", "REVIEW_REQUIRED")}}
    return {"total_kg_entities": len(rows), "aligned_kg_entities": statuses["ALIGNED"],
            "alignment_coverage": statuses["ALIGNED"] / len(rows) if rows else 0.0,
            "MAPS_TO": relations["MAPS_TO"], "INSTANCE_OF": relations["INSTANCE_OF"],
            "UNMATCHED": statuses["UNMATCHED"], "REVIEW_REQUIRED": statuses["REVIEW_REQUIRED"],
            "class_inclusion_review_entities": sum(bool(r["class_inclusion_proposals"]) for r in rows),
            "by_entity_type": by_type,
            "scope": "Only the selected KG snapshot; not an estimate of full-database coverage or accuracy.",
            "validation": "Automated evidence checks; no independent expert gold standard."}


def _export(output, rows):
    atomic_json(output / "alignment.json", {"version": VERSION, "rows": rows})
    # Persist JSON evidence in CSV as a single escaped cell for lossless tracing.
    fields = ["kg_node_id", "kg_name", "entity_type", "ontology_node_id", "ontology_name",
              "relation_type", "decision_status", "evidence"]
    temporary = output / ".alignment.csv.tmp"
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "evidence": json.dumps(row["evidence"], ensure_ascii=False)})
    temporary.replace(output / "alignment.csv")
    summary = summarize(rows)
    atomic_json(output / "summary.json", summary)
    return summary


def run_alignment(ontology, kg_snapshot, output, *, model=None, embeddings=None,
                  max_candidates=6, max_reference_candidates=2, use_ols=True,
                  lexical_min_score=.15, embedding_min_score=.5, reference_lookup=None,
                  progress=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    nodes, kg_nodes = ontology["nodes"], kg_snapshot["nodes"]
    ontology_by_id = {node["id"]: node for node in nodes}
    if len(ontology_by_id) != len(nodes) or len({n["id"] for n in kg_nodes}) != len(kg_nodes):
        raise ValueError("Input node IDs must be unique within each graph")
    if not 1 <= max_reference_candidates <= max_candidates:
        raise ValueError("Reference candidate budget must be between 1 and max_candidates")
    settings = {"version": VERSION, "max_candidates": max_candidates,
                "max_reference_candidates": max_reference_candidates, "use_ols": use_ols,
                "lexical_min_score": lexical_min_score, "embedding_min_score": embedding_min_score,
                "model": model.settings if model else None,
                "embedding": embeddings.config if embeddings else None,
                "alignment_prompt": getattr(model, "prompt_name", "legacy"),
                "prompt_sha256": digest(getattr(model, "policy", POLICY))}
    # Authentication is environment-only. Embedding configs must follow that contract.
    if settings["embedding"] and any(k in settings["embedding"] for k in ("api_key", "password", "authorization")):
        raise ValueError("Embedding credentials must be supplied through environment variables")
    signature = digest({"ontology": nodes, "kg": kg_nodes, "settings": settings})
    retriever = CandidateRetriever(nodes, max_candidates=max_candidates, embeddings=embeddings,
                                   lexical_min_score=lexical_min_score, embedding_min_score=embedding_min_score)
    if model and use_ols and reference_lookup is None:
        reference_lookup = EntityReferenceLookup(output / "ols", ols=OLSReferenceAgent(model.reference_request),
                                                 settings=model.settings)
    with run_lock(output):
        manifest_path = output / "manifest.json"
        if manifest_path.exists() and read_json(manifest_path).get("input_signature") != signature:
            raise ValueError("Output belongs to different inputs/settings. Choose a new output directory.")
        manifest = {"version": VERSION, "input_signature": signature, "settings": settings,
                    "ontology_metadata": ontology.get("metadata", {}),
                    "kg_metadata": kg_snapshot.get("metadata", {}),
                    "started_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
                    "writes_to_neo4j": False, "ontology_expansion": False}
        atomic_json(manifest_path, manifest)
        atomic_json(output / "ontology_snapshot.json", ontology)
        atomic_json(output / "kg_snapshot.json", kg_snapshot)
        rows = []
        for position, kg in enumerate(kg_nodes, 1):
            if model:
                model.journal.last_ref = None
                model.last_call_refs = []
            row = {"kg_node_id": kg["id"], "kg_name": kg["name"], "entity_type": kg["kind"],
                   "ontology_node_id": None, "ontology_name": None, "relation_type": "UNMATCHED",
                   "decision_status": "UNMATCHED", "class_inclusion_proposals": [],
                   "evidence": {"kg_source": kg.get("source"), "candidate_retrieval": [],
                                "assessments": [], "model_call_refs": []}}
            evidence = row["evidence"]
            try:
                candidates = retriever.retrieve(kg)
                retrieval = retriever.retrieval_summary(kg["id"])
                evidence.update(candidate_retrieval=candidates, retrieval_summary=retrieval)
                if not candidates:
                    evidence["reason"] = "no_candidates_within_type_and_retrieval_thresholds"
                elif not model:
                    row["decision_status"] = "REVIEW_REQUIRED"
                    evidence["reason"] = "retrieval_only_no_llm_adjudication"
                else:
                    payload = {"kg": _compact(kg), "items": [
                        {**_compact(ontology_by_id[c["ontology_node_id"]]), "retrieval": c["retrieval"],
                         "parents": [_compact(ontology_by_id[p]) for p in
                                     ontology_by_id[c["ontology_node_id"]].get("parent_ids", [])
                                     if p in ontology_by_id]} for c in candidates]}
                    assessments = model.assess(payload)
                    evidence["model_call_refs"].extend(getattr(model, "last_call_refs", [model.journal.last_ref]))
                    admitted = [admit(a, kg, ontology_by_id[a["id"]]) for a in assessments]
                    evidence["initial_assessments"] = admitted
                    uncertain = [a for a in admitted if not a["accepted"] and a["reason"] != "incompatible"]
                    if uncertain and reference_lookup:
                        selected = uncertain[:max_reference_candidates]
                        evidence["reference_candidate_ids"] = [a["ontology_node_id"] for a in selected]
                        questions = [reference_pair(kg, ontology_by_id[a["ontology_node_id"]]) for a in selected]
                        reference = reference_lookup(kg, questions)
                        evidence["external_reference"] = reference
                        assessments = model.assess({**payload, "external_reference": reference})
                        evidence["model_call_refs"].extend(getattr(model, "last_call_refs", [model.journal.last_ref]))
                        admitted = [admit(a, kg, ontology_by_id[a["id"]], reference) for a in assessments]
                    evidence["assessments"] = admitted
                    row["class_inclusion_proposals"] = [a["class_inclusion_proposal"] for a in admitted
                                                        if "class_inclusion_proposal" in a]
                    selected, status, reason = choose(admitted, ontology_by_id, retrieval)
                    row["decision_status"], evidence["reason"] = status, reason
                    if selected:
                        oid = selected["ontology_node_id"]
                        row.update(ontology_node_id=oid, ontology_name=ontology_by_id[oid]["name"],
                                   relation_type=selected["assessment"]["relation_type"])
            except (ProviderError, BudgetExceeded, ContractError, ValueError, OSError) as exc:
                # Service failures are not semantic nonmatches and never create edges.
                row["decision_status"] = "REVIEW_REQUIRED"
                evidence.update(reason="alignment_service_or_contract_error", error_type=type(exc).__name__)
                if model:
                    evidence["model_call_refs"] = list(dict.fromkeys(
                        evidence["model_call_refs"] + getattr(model, "last_call_refs", [])
                        + ([model.journal.last_ref] if model.journal.last_ref else [])))
            rows.append(row)
            atomic_json(output / "records" / (digest(kg["id"]) + ".json"), row)
            if progress:
                progress(position, len(kg_nodes), row)
        summary = _export(output, rows)
        atomic_json(manifest_path, {**manifest, "status": "complete", "summary": summary,
                                    "completed_utc": datetime.now(timezone.utc).isoformat()})
    return summary
