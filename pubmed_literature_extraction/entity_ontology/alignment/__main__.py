"""CLI: inspect, snapshot, then align existing artifacts without graph writes."""

import argparse
import json
import os
from pathlib import Path
import sys

from ..concepts.embedding import EmbeddingAPI, Embeddings
from ..model import DEFAULT_CONFIG, endpoint, live_provider, read_settings
from ..storage import atomic_json, digest, read_json
from .loading import inspect_neo4j, load_kg_snapshot, load_ontology, read_neo4j
from .pipeline import AlignmentModel, run_alignment
from .prompts import DEFAULT_PROMPT, PROMPT_VARIANTS


def _connection(parser):
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password-env", default="NEO4J_PASSWORD")
    parser.add_argument("--database", required=True, help="Explicit database; no silent default database")


def _embeddings(config, output):
    cfg = dict(read_json(config)["embedding"])
    cfg["base_url"] = endpoint(cfg)
    connection, parameters = read_settings(config, "audit_edges")
    if (cfg["base_url"].rstrip("/") != endpoint(connection).rstrip("/")
            or cfg["api_key_env"] != connection["api_key_env"]):
        raise ValueError("Use the existing shared LLM/embedding endpoint and credential configuration")
    live_provider(connection, parameters)
    directory = Path(output) / "embeddings" / digest(cfg)[:20]
    directory.mkdir(parents=True, exist_ok=True)
    embeddings = Embeddings(EmbeddingAPI(cfg), cfg, directory)
    embeddings.restore()
    return embeddings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Read Neo4j counts and property schema")
    _connection(inspect)
    snapshot = commands.add_parser("snapshot", help="Export a read-only stratified KG sample")
    _connection(snapshot)
    snapshot.add_argument("--limit-per-type", type=int, default=5)
    snapshot.add_argument("--types", nargs="+")
    snapshot.add_argument("--output", required=True, type=Path)
    align = commands.add_parser("run", help="Align snapshots; writes only local result files")
    align.add_argument("--ontology", required=True, type=Path, help="Existing placement.json")
    align.add_argument("--equivalence", type=Path)
    align.add_argument("--kg-snapshot", required=True, type=Path)
    align.add_argument("--output", required=True, type=Path)
    align.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    align.add_argument("--alignment-prompt", choices=list(PROMPT_VARIANTS), default=DEFAULT_PROMPT,
                       help="Versioned alignment prompt; use a new output directory when changing it")
    align.add_argument("--retrieval-only", action="store_true", help="No model calls; every candidate awaits review")
    align.add_argument("--embedding", action="store_true", help="Use existing embedding service when exact lookup misses")
    align.add_argument("--no-ols", action="store_true")
    align.add_argument("--max-candidates", type=int, default=6)
    align.add_argument("--max-reference-candidates", type=int, default=2)
    align.add_argument("--max-llm-calls", type=int, default=200)
    align.add_argument("--lexical-min-score", type=float, default=.15)
    align.add_argument("--embedding-min-score", type=float, default=.5)
    args = parser.parse_args(argv)
    try:
        if args.command in {"inspect", "snapshot"}:
            credentials = (args.neo4j_uri, args.neo4j_user, os.environ.get(args.neo4j_password_env))
            if args.command == "inspect":
                result = inspect_neo4j(*credentials, database=args.database)
            else:
                if args.output.exists():
                    raise ValueError("Snapshot output already exists; choose a new path to preserve it")
                result = read_neo4j(*credentials, database=args.database, limit_per_type=args.limit_per_type,
                                    types=args.types)
                atomic_json(args.output, result)
                result = result["metadata"]
        else:
            if args.max_llm_calls < 1:
                raise ValueError("max-llm-calls must be positive")
            if args.retrieval_only and args.embedding:
                raise ValueError("retrieval-only makes no service calls; omit --embedding")
            ontology = load_ontology(args.ontology, args.equivalence)
            kg = load_kg_snapshot(args.kg_snapshot)
            # Never allow result snapshot names to replace original input files.
            destinations = {args.output.resolve() / n for n in ("ontology_snapshot.json", "kg_snapshot.json")}
            if args.ontology.resolve() in destinations or args.kg_snapshot.resolve() in destinations:
                raise ValueError("Use an output directory separate from the input snapshots")
            model = None if args.retrieval_only else AlignmentModel(
                args.config, args.output, args.max_llm_calls, prompt_name=args.alignment_prompt)
            embeddings = _embeddings(args.config, args.output) if args.embedding else None
            result = run_alignment(ontology, kg, args.output, model=model, embeddings=embeddings,
                max_candidates=args.max_candidates, max_reference_candidates=args.max_reference_candidates,
                use_ols=not args.no_ols, lexical_min_score=args.lexical_min_score,
                embedding_min_score=args.embedding_min_score,
                progress=lambda i, n, r: print(f"[{i}/{n}] {r['entity_type']} {r['kg_name']}: {r['decision_status']}",
                                                flush=True))
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as exc:
        # Network exceptions may include request details. Keep terminal errors safe;
        # model request journals already capture sanitized provider errors.
        message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else type(exc).__name__
        print("Alignment failed: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
