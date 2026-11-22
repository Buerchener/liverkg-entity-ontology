<div align="center">

# LiverTrail

### A stage-aware liver disease knowledge graph that grows from PubMed without losing provenance

[简体中文](README.zh-CN.md) · [Extraction agent](pubmed_literature_extraction/README.md) · [Human review](HUMAN_REVIEW.md) · [Result releases](https://github.com/Buerchener/liverkg-entity-ontology/releases)

</div>

LiverTrail combines curated biomedical databases with knowledge extracted from PubMed titles and abstracts, organised around the progression of chronic liver disease:

```text
NAFLD → NASH → Fibrosis → Cirrhosis → HCC
```

A model may propose knowledge, but only schema-valid, source-grounded and deterministically verified evidence may reach the write path. Each literature-derived concept links back to the sentences it came from, and each proposed hierarchy edge has a placement record that explains how it was assessed.

Code, commands and databases keep their original `liverkg` names, for example the `liverkg` command, the `liverkg_cli` package and the `liver-kg-core-v02` database.

[Four layers](#four-layers) · [Explore the published graph](#explore-the-published-graph) · [Run the pipeline](#run-the-pipeline) · [Methods](#methods) · [100-article results](#100-article-results) · [Tests and safety](#reproducibility-tests-and-safety)

## Four layers

| Layer | What it does | Code |
| --- | --- | --- |
| A. Curated backbone | Anchors five disease stages to UMLS concepts and integrates DisGeNET, STRING, KEGG, Reactome, the Human Protein Atlas and HMDB into a Neo4j graph of 3,546 nodes and 38,203 relationships. | [`scripts/`](scripts), [`data/`](data), [`review/`](review) |
| B. Extraction agent | Extracts entities and typed relations from abstracts. A deterministic verifier checks schema signatures, verbatim evidence spans, negation, hedging and claim scope, and a decision engine assigns every relation a write status. | [`cognitive_agent/`](pubmed_literature_extraction/cognitive_agent), [`liverkg_cli/`](pubmed_literature_extraction/liverkg_cli) |
| C. Entity ontology | Resolves mentions across papers into concepts and proposes `is_a` relationships within seven entity types, with independent review, external ontology evidence and structural validation. | [`entity_ontology/`](pubmed_literature_extraction/entity_ontology) |
| D. Integration | Links concepts to backbone nodes, projects verified relations onto concepts, applies a concept-level gate, labels each relation as known in the backbone or new, exports graph files and loads approved relations into an isolated Neo4j database. | [`linking/`](pubmed_literature_extraction/entity_ontology/linking), [`relations/`](pubmed_literature_extraction/entity_ontology/relations), [`export/`](pubmed_literature_extraction/entity_ontology/export) |

The extraction agent, the ontology workflow and the integration layer form one Python package in [`pubmed_literature_extraction/`](pubmed_literature_extraction) with a single command, `liverkg`.

**KG-guided enrichment (Evidence-v4).** A separate workflow places unmatched LiverKG nodes in a copy of the literature ontology. In a 105-node experiment, 63 mappings and 47 specific hierarchy edges passed the release review. [Run the enrichment pipeline](pubmed_literature_extraction/entity_ontology/enrichment/README.md) · [Experiment report and reviewed results (Chinese)](pubmed_literature_extraction/benchmark_output/ontology-enrichment-evidence-v4/report.zh.md)

## Explore the published graph

One abstract reports a change in the expression of **collagen I**. LiverTrail records that occurrence as `41692141:M05` and links it to the protein concept `C-ad58a8434684`. The abstract supplies evidence for the mention. The hierarchy builder separately proposes *extracellular matrix proteins* as its parent. [Source abstract](https://pubmed.ncbi.nlm.nih.gov/41692141/) · [Versioned graph release](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-ols-agent-v1)

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

Use Python 3.12 or 3.13 and Git on macOS or Linux. A new run needs an OpenAI-compatible chat API. The example configuration uses `qwen3.7-flash` for language tasks and `text-embedding-v4` for retrieval. With a chat model only, start from the single-model configuration described below. Extraction always runs in dry-run mode and writes results to local files; nothing is written to a database unless you run `load`.

### Install and configure

```bash
git clone https://github.com/Buerchener/liverkg-entity-ontology.git
cd liverkg-entity-ontology/pubmed_literature_extraction
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[ontology]'
cp entity_ontology/config.example.json entity-ontology.local.json
```

Set your provider's API base URL, which must end in `/v1`, and enter your key when the shell waits for input.

```bash
export LIVERKG_API_BASE="https://<provider-host>/v1"
read -rs LIVERKG_API_KEY
export LIVERKG_API_KEY
```

**Single-model mode.** Copy `entity_ontology/config.single-model.example.json` instead and replace `YOUR_CHAT_MODEL` with your model name. The extractor's second model and auxiliary judges then reuse that model unless separate credentials are supplied, and synonym candidates come from local character n-gram hashing vectors instead of an embedding service. These vectors find spelling variants but not synonyms without shared characters; the model still decides every merge. The run manifest records which model filled which role.

### Process one article

The repository includes a 500-article PubMed corpus. Select the article used in the example above and run it as a single-article batch.

```bash
mkdir -p inputs
grep -m 1 '"pmid": "41692141"' extraction_output/pubmed_converted_500.jsonl > inputs/example.jsonl
liverkg ontology run --articles inputs/example.jsonl --run-id collagen-example
liverkg ontology serve --open
liverkg ontology load --run-id collagen-example --database liverkg-integrated --dry-run
```

`python -m entity_ontology` accepts the same subcommands as `liverkg ontology`. The read-only workbench opens at `http://127.0.0.1:4320`. Select a completed batch to inspect source sentences, review synonym decisions, expand the hierarchy, or browse backbone links and concept-level relations. A single-article run has a smaller pool of candidate parents than the published 100-article graph. The `load` command with `--dry-run` only reports what would be written.

For your own corpus, supply a UTF-8 JSONL file with one article per line. Each record needs a unique numeric `pmid` and nonempty `title` and `abstract` strings. Outputs for the example are under `runs/entity_ontology/collagen-example/`. To open complete batches stored elsewhere, use `liverkg ontology serve --runs-dir <batch-parent-directory>`.

## Methods

The pipeline covers diseases, genes, proteins, metabolites, pathways, tissues, and cell types. Within each type it constructs a directed acyclic graph (DAG) with one root. The batch runner executes nine stages in order and writes these artifacts. [Pipeline entry point](pubmed_literature_extraction/entity_ontology/pipeline.py)

| Stage | Operation | Artifact |
| --- | --- | --- |
| [Extraction](pubmed_literature_extraction/entity_ontology/extraction/native.py) | Run the extraction agent in dry-run mode. Keep entities, verified relations with their evidence offsets, and each relation's write status. | `agent_results.json` |
| [Coverage check](pubmed_literature_extraction/entity_ontology/extraction/coverage.py) | Build the mention inventory with PMIDs, sentences, and character offsets. Check measurement contexts for omissions and verify the span of each addition against the article. | `entity_coverage.json`, `input_mentions.json` |
| [Conceptualization](pubmed_literature_extraction/entity_ontology/concepts/normalization.py) | Interpret each mention in context, assign a name, type, definition, and aliases, then independently review its type. Record ambiguous and out-of-scope cases. | `normalization.json` |
| [Candidate retrieval](pubmed_literature_extraction/entity_ontology/concepts/retrieve_pairs.py) and [equivalence review](pubmed_literature_extraction/entity_ontology/concepts/deduplicate.py) | Combine cosine nearest neighbors (default *k* = 2) with pairs that share a name or alias; the workbench also displays HDBSCAN clusters for exploration. Judge whether two mentions denote the same entity at the same level of specificity. A shared name or alias across types triggers a fresh type check against each source sentence. Merge a group only when every pair is confirmed equivalent. If all pairs but one are confirmed and the last has no verdict, review it separately. | `retrieval/retrieved_pairs.json`, `equivalence.json` |
| [Concept ordering](pubmed_literature_extraction/entity_ontology/hierarchy/order_entities.py) | Rank concepts from broad to narrow within each type to determine their insertion order. | `concept_order.json` |
| [Hierarchy placement](pubmed_literature_extraction/entity_ontology/hierarchy/placement.py) | Propose and independently review parent–child relationships. Check type consistency, cycles, and reachability from the type root before committing an update. Record placements and unresolved cases. | `incremental-placement/placement.json` |
| [Concept linking](pubmed_literature_extraction/entity_ontology/linking/kg_links.py) | Match concept names, aliases and identifiers against an offline index of the v2 data package. Mark each concept `LINKED`, `AMBIGUOUS` or `NEW`. | `concept_links.json` |
| [Relation projection](pubmed_literature_extraction/entity_ontology/relations/project.py) | Resolve both endpoints of every verified relation to concepts, aggregate evidence across articles, apply the concept-level gate and label novelty against the backbone. | `concept_relations.json` |
| [Graph export](pubmed_literature_extraction/entity_ontology/export/graph.py) | Write articles, mentions, evidence, concepts, `is_a` edges, relations and `SAME_AS` links, with a manifest of SHA-256 hashes. | `graph/*.jsonl` |

### External ontology evidence

When external lookup is enabled and a placement question remains unresolved, the **OLS Reference Agent** selects a source according to the entity type and question. It can consult GO, MONDO, the Protein Ontology, ChEBI, Uberon, the Cell Ontology, and HGNC records for human gene identity. Each target concept has up to five decision rounds. The model chooses whether to search, inspect a term, verify a concept, or stop. Python executes the action and returns the observation to the next round. Search uses OLS MCP with a REST fallback. Official term records and direct parents are retrieved through REST. [Agent decision loop](pubmed_literature_extraction/entity_ontology/hierarchy/ols_reference.py) · [OLS queries](pubmed_literature_extraction/entity_ontology/hierarchy/ontology_ols.py) · [MCP client](pubmed_literature_extraction/entity_ontology/hierarchy/ols_client.py)

To support a strict parent–child claim, the workflow requires unambiguous mappings for both concepts in the same source and a direct `is_a` edge in the proposed direction. A shared external identifier can support an identity assessment. Qualifying evidence returns to the placement reviewer. [Evidence selection](pubmed_literature_extraction/entity_ontology/hierarchy/entity_reference.py)

### Gate, novelty and loading

The concept-level gate can only keep or lower the extractor's decision. `AUTO` needs at least one `IMPORT_READY` evidence item, no negation, counter-evidence or statistical non-confirmation, placed concepts on both ends and endpoint types that match the concept types. Otherwise a relation is `REVIEW`, with its reasons in `gate_reasons`, or `BLOCKED`. `novelty` compares each relation with the v2 package: `KNOWN_IN_V2`, `NEW` or `UNLINKED`. `NEW` means "not in the curated backbone", not "new to science".

`load` is a separate command that the pipeline never calls. It accepts only `localhost`, `127.0.0.1` or `::1` URIs and an explicitly named database other than `liver-kg-core-v02`, verifies every export file against its hash, uses `MERGE` only and loads `AUTO` relations. Linked concepts get `SAME_AS` edges to existing v2 nodes, or to `:V2Anchor` nodes carrying the v2 key when the database holds no v2 copy. [Loader](pubmed_literature_extraction/entity_ontology/export/neo4j_load.py)

## 100-article results

The unified OLS release replays placement for the same **100 articles and 545 ordered concepts** as its baseline. It reuses matching proposals and ordinary reviews, then reruns external lookup and evidence-informed review. Both releases predate the cross-type alias checks and missing-pair reviews, and the merge of the integration layer, so they contain hierarchies but no concept-level relations. [Unified OLS release and checksums](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-ols-agent-v1) · [Baseline release](https://github.com/Buerchener/liverkg-entity-ontology/releases/tag/results-entity-100-reference-v2)

| Observed in the unified OLS run | Count |
| --- | ---: |
| Literature-derived concepts placed in the graph | 370 |
| Candidate edges between concepts | 171 |
| Target concepts investigated through OLS | 58 |
| Logged lookup actions | 164 (39 MCP, 125 REST) |

The graph also contains seven type roots and 240 links to those roots. Both runs retained the same 171 concept-to-concept edges. The unified agent found direct external `is_a` support for **0 of those 171 edges**. Assessing edge accuracy requires an independently annotated reference set. The release includes the graph and the comparison report. The full workbench also reads intermediate files from a completed local batch. [Download the comparison report](https://github.com/Buerchener/liverkg-entity-ontology/releases/download/results-entity-100-ols-agent-v1/ols-reference-comparison.json)

## Knowledge graph scope

| Layer | Current scale | Primary source |
| --- | ---: | --- |
| Disease | 5 | UMLS-aligned backbone |
| Gene | 836 | DisGeNET |
| Protein | 793 | STRING |
| Pathway | 1,721 | KEGG, Reactome |
| Tissue | 1 | HPA |
| Cell type | 154 | HPA single-cell data |
| Metabolite | 36 | HMDB |
| Gene–disease associations | 1,036 | DisGeNET |
| Protein–protein interactions | 7,154 | STRING |
| Gene–pathway memberships | 9,947 | KEGG, Reactome |
| HPA expression relations | 17,745 | HPA |
| HPA LIHC prognostic relations | 1,384 | HPA |
| Gene–metabolite relations | 97 | HMDB |

These figures describe the curated v2 data package. PubMed-derived statements remain candidates until they pass the evidence, schema and import-readiness gates.

## Repository layout

```text
.
├── data/                          # Curated v2 data package (Layer A)
├── scripts/                       # Data acquisition, normalisation and Neo4j import (Layer A)
├── review/                        # Backbone schema, scope audit and caveats
├── archive/                       # Historical reports and migration artefacts
├── pubmed_literature_extraction/  # The Python package and the liverkg command
│   ├── cognitive_agent/           # Extraction agent (Layer B)
│   ├── liverkg_cli/               # Command-line interface
│   ├── entity_ontology/           # Ontology workflow (Layer C) and integration (Layer D)
│   ├── tests/                     # Extraction agent tests
│   └── extraction_output/         # Bundled PubMed corpus and earlier extraction results
├── HUMAN_REVIEW.md                # Recommended review entry point
└── MANIFEST.txt                   # Package inventory
```

## Reproducibility, tests and safety

- **Resume a run.** Repeat the command with the same inputs, model configuration, and run ID. The batch manifest records the hashes of the input articles, the model configuration and the v2 data package, the extractor's Git commit, the component versions, stage status, and completion counts. Use a new run ID to compare a changed policy against an existing batch.
- **Control an experiment.** Set `--knn-k` to change the neighbor count. Use `--no-coverage` to skip the coverage check, `--no-entity-reference` to disable external lookup, `--no-go-reference` to exclude GO alone, or `--v2-data` to point concept linking at another copy of the v2 package.
- **Run the tests.** The suites use synthetic inputs and simulated services. The 13 BioRED adapter tests need a local copy of the BioRED corpus, which is not distributed with the repository.

```bash
cd pubmed_literature_extraction
python -m unittest discover -s tests                   # extraction agent
python -m unittest discover -s entity_ontology/tests   # ontology and integration
```

- The default execution path is dry-run; Neo4j writes require the separate, confirmed `load` command.
- API keys, passwords, local environments, model configurations, caches and run outputs are ignored by Git.
- External graph context is an entity-linking or conflict hint, never article evidence.
- Every model-produced modification is passed through deterministic verification.
- Isolated Neo4j integration tests never fall back to a production database.

## Evidence boundaries

- DisGeNET associations form the principal disease-evidence backbone.
- STRING interactions are molecular context, not direct disease causality.
- KEGG/Reactome membership does not imply disease-stage specificity.
- HPA cell-type observations are pan-tissue context unless explicitly liver-specific.
- HMDB relations are conservatively filtered disease context, not a complete HMDB import.
- Hierarchy edges and literature relations are provisional research results until they are evaluated against independently annotated reference data.

## Project status

The curated v2 package has passed the backbone scope audit. The extraction agent, the ontology workflow and the integration layer run end to end as one pipeline. The next stage is a quantitative evaluation of extraction, ontology and integration quality. For review, start with [HUMAN_REVIEW.md](HUMAN_REVIEW.md) and the [current caveats](review/04_caveats_and_next_cleanup.md).
