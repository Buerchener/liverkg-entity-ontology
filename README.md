# LiverKG Entity Ontology

LiverKG builds a candidate ontology of biomedical entities from PubMed titles and abstracts on liver disease. It resolves mentions across papers into concepts, then proposes `is_a` relationships within seven entity types. Each literature-derived concept links back to the sentences it came from. Each proposed relationship has a placement record that explains how it was assessed.

[Explore the published graph](#explore-the-published-graph) · [Run the pipeline](#run-the-pipeline) · [Methods](#methods) · [100-article results](#100-article-results)

## Explore the published graph

One abstract reports a change in the expression of **collagen I**. LiverKG records that occurrence as `41692141:M05` and links it to the protein concept `C-ad58a8434684`. The abstract supplies evidence for the mention. The hierarchy builder separately proposes *extracellular matrix proteins* as its parent. [Source abstract](https://pubmed.ncbi.nlm.nih.gov/41692141/) · [Versioned graph release](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-ols-agent-v1)

To inspect that edge, run this example in an empty directory. It uses only `curl`, Python 3, and the public graph file.

```bash
curl -fsSLO https://github.com/Buerchener/liverkg-entity-ontology/releases/download/results-entity-100-ols-agent-v1/placement.json
python3 - <<'PY'
import json
from pathlib import Path

graph = json.loads(Path("placement.json").read_text())
names = {node["id"]: node["name"] for node in graph["nodes"]}
for edge in graph["edges"]:
    if edge["child_id"] == "C-ad58a8434684":
        print(names[edge["child_id"]], edge["relation"], names[edge["parent_id"]])
        print(edge["support"])
PY
```

```text
collagen I is_a extracellular matrix proteins
dual_model_review
```

The edge points from child to parent. Here, `dual_model_review` means that a model proposed the relationship and a separate model review supported it. A concept can have several direct parents.

## Run the pipeline

Use Python 3.12 or 3.13 and Git on macOS or Linux. For a new run, you need a service that supports both chat completions and embeddings through an OpenAI-compatible API. The example configuration uses `qwen3.7-flash` for language tasks and `text-embedding-v4` for retrieval. Extraction runs in dry-run mode and writes results to local files. [Model configuration](entity_ontology/config.example.json) · [Pinned LiverKG extractor](https://github.com/Buerchener/FYP-liver-disease/commit/fc9ebd72971318a7738f15fc2b86369a552656c2)

### Install and configure

```bash
git clone https://github.com/Buerchener/liverkg-entity-ontology.git
cd liverkg-entity-ontology
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r entity_ontology/requirements.txt
python -m entity_ontology setup-extractor
cp entity_ontology/config.example.json entity-ontology.local.json
```

Set your provider's API base URL and enter your key when the shell waits for input.

```bash
export LIVERKG_API_BASE="https://<provider-host>/v1"
read -rs LIVERKG_API_KEY
export LIVERKG_API_KEY
```

### Process one article

The pinned extractor includes the article used in the example above. Select it from the bundled corpus and run it as a single-article batch.

```bash
mkdir -p inputs
extractor_dir=.cache/entity_ontology/extractor/fc9ebd72971318a7738f15fc2b86369a552656c2
grep -m 1 '"pmid": "41692141"' \
  "$extractor_dir/pubmed_literature_extraction/extraction_output/pubmed_converted_500.jsonl" \
  > inputs/example.jsonl
python -m entity_ontology run --articles inputs/example.jsonl --run-id collagen-example
python -m entity_ontology serve --open
```

The read-only workbench opens at `http://127.0.0.1:4320`. Select a completed batch to inspect source sentences, review synonym decisions, or expand the hierarchy. A single-article run has a smaller pool of candidate parents than the published 100-article graph.

For your own corpus, supply a UTF-8 JSONL file with one article per line. Each record needs a unique numeric `pmid` and nonempty `title` and `abstract` strings. Outputs for the example are under `runs/entity_ontology/collagen-example/`. To open complete batches stored elsewhere, use `python -m entity_ontology serve --runs-dir <batch-parent-directory>`.

## Methods

The pipeline covers diseases, genes, proteins, metabolites, pathways, tissues, and cell types. It constructs a directed acyclic graph (DAG) with one root per type. The batch runner writes the following artifacts in order. [Pipeline entry point](entity_ontology/pipeline.py)

| Stage | Operation | Artifact |
| --- | --- | --- |
| [Mention extraction](entity_ontology/extraction/native.py) and [coverage check](entity_ontology/extraction/coverage.py) | Extract mentions with their PMIDs, sentences, and character offsets. Check measurement contexts for omissions and verify the span of each addition against the article. | `input_mentions.json` |
| [Conceptualization](entity_ontology/concepts/normalization.py) | Interpret each mention in context, assign a name, type, definition, and aliases, then independently review its type. Record ambiguous and out-of-scope cases. | `normalization.json` |
| [Candidate retrieval](entity_ontology/concepts/retrieve_pairs.py) | Combine cosine nearest neighbors (default *k* = 2) with pairs that share a name or alias. The workbench also displays HDBSCAN clusters for exploration. | `retrieval/retrieved_pairs.json` |
| [Equivalence review](entity_ontology/concepts/deduplicate.py) | Judge whether two mentions denote the same entity at the same level of specificity. A shared name or alias across types triggers a fresh type check against each source sentence. Merge a group only when every pair is confirmed equivalent. If all pairs but one are confirmed and the last has no verdict, review it separately. | `equivalence.json` |
| [Concept ordering](entity_ontology/hierarchy/order_entities.py) | Rank concepts from broad to narrow within each type to determine their insertion order. | `concept_order.json` |
| [Hierarchy placement](entity_ontology/hierarchy/placement.py) | Propose and independently review parent–child relationships. Check type consistency, cycles, and reachability from the type root before committing an update. Record placements and unresolved cases. | `incremental-placement/placement.json` |

### External ontology evidence

When external lookup is enabled and a placement question remains unresolved, the **OLS Reference Agent** selects a source according to the entity type and question. It can consult GO, MONDO, the Protein Ontology, ChEBI, Uberon, the Cell Ontology, and HGNC records for human gene identity. Each target concept has up to five decision rounds. The model chooses whether to search, inspect a term, verify a concept, or stop. Python executes the action and returns the observation to the next round. Search uses OLS MCP with a REST fallback. Official term records and direct parents are retrieved through REST. [Agent decision loop](entity_ontology/hierarchy/ols_reference.py) · [OLS queries](entity_ontology/hierarchy/ontology_ols.py) · [MCP client](entity_ontology/hierarchy/ols_client.py)

To support a strict parent–child claim, the workflow requires unambiguous mappings for both concepts in the same source and a direct `is_a` edge in the proposed direction. A shared external identifier can support an identity assessment. Qualifying evidence returns to the placement reviewer. [Evidence selection](entity_ontology/hierarchy/entity_reference.py)

## 100-article results

The unified OLS release replays placement for the same **100 articles and 545 ordered concepts** as its baseline. It reuses matching proposals and ordinary reviews, then reruns external lookup and evidence-informed review. Both releases predate the cross-type alias checks and missing-pair reviews on the current main branch. [Unified OLS release and checksums](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-ols-agent-v1) · [Baseline release](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-reference-v2)

| Observed in the unified OLS run | Count |
| --- | ---: |
| Literature-derived concepts placed in the graph | 370 |
| Candidate edges between concepts | 171 |
| Target concepts investigated through OLS | 58 |
| Logged lookup actions | 164 (39 MCP, 125 REST) |

The graph also contains seven type roots and 240 links to those roots. Both runs retained the same 171 concept-to-concept edges. The unified agent found direct external `is_a` support for **0 of those 171 edges**. Assessing edge accuracy requires an independently annotated reference set. The release includes the graph and the comparison report. The full workbench also reads intermediate files from a completed local batch. [Download the comparison report](https://github.com/Buerchener/liverkg-entity-ontology/releases/download/results-entity-100-ols-agent-v1/ols-reference-comparison.json)

## Reproducibility and tests

- **Resume a run.** Repeat the command with the same inputs, model configuration, and run ID. The batch manifest records input and configuration hashes, stage status, and completion counts. Use a new run ID to compare a changed policy against an existing batch.
- **Control an experiment.** Set `--knn-k` to change the neighbor count. Use `--no-coverage` to skip the coverage check, `--no-entity-reference` to disable external lookup, or `--no-go-reference` to exclude GO alone.
- **Run the tests.** The suite uses synthetic inputs and simulated services to exercise equivalence review, evidence selection, and multi-parent placement.

```bash
python -m unittest discover -s entity_ontology/tests
```
