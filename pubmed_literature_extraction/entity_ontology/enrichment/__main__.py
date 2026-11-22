"""Command line for the bounded, read-only enrichment experiment."""

import argparse
import json
from pathlib import Path

from .pipeline import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ontology", type=Path, required=True)
    parser.add_argument("--ontology-grounding", type=Path)
    parser.add_argument("--kg-snapshot", type=Path, required=True)
    parser.add_argument("--alignment", type=Path, action="append", required=True)
    parser.add_argument("--cell-ontology", type=Path)
    parser.add_argument("--uberon", type=Path)
    parser.add_argument("--chebi", type=Path)
    parser.add_argument("--mondo", type=Path)
    parser.add_argument("--articles", type=Path, action="append", default=[])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--sample-size", type=int, default=42)
    selection.add_argument("--cohort", type=Path, help="Use an independently frozen cohort manifest.")
    parser.add_argument("--seed", type=int, default=20261122)
    parser.add_argument("--max-llm-calls", type=int, default=420)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--no-ols", action="store_true")
    options = parser.parse_args()

    def progress(record):
        kg = record["kg"]
        print(f"{kg['kind']} | {kg['name']} | {record['status']} | {record['reason']}", flush=True)

    result = run(
        ontology_path=options.ontology, kg_snapshot=options.kg_snapshot,
        alignment_paths=options.alignment, ontology_grounding=options.ontology_grounding,
        config=options.config, output=options.output, sample_size=options.sample_size,
        seed=options.seed, maximum_calls=options.max_llm_calls, articles=options.articles,
        use_ols=not options.no_ols, workers=options.workers, progress=progress, cohort_path=options.cohort,
        obo_paths={kind: path for kind, path in {
            "CellType": options.cell_ontology, "Tissue": options.uberon,
            "Metabolite": options.chebi, "Disease": options.mondo,
        }.items() if path},
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
