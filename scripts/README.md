# Tools Index

This directory contains import and utility scripts for the liver disease
knowledge graph. The current active build is `liver-kg-core-v02`.

## Offline v2 baseline

Run the build from a Python environment with LiverKG installed. The command
reads the curated TSVs under the repository's data directory. The preview is
the default mode. Use the explicit database name for the local import.

~~~bash
liverkg backbone build --dry-run
liverkg backbone build --apply --database neo4j --report /tmp/livertrail-backbone-v2.json
~~~

Set NEO4J_PASSWORD for the local instance in the process environment.
NEO4J_USER defaults to neo4j. Use --v2-data to select another copy of the
frozen TSVs.

The report preserves SHA-256 and row counts for each input table. Source
versions identify the exact TSV used. The plan checks all edge endpoints
before writing. The loader uses the shared node keys and typed relation-ID
contract with Neo4j uniqueness constraints. It applies graph records in one
transaction and includes the observed database counts and write counters.
The [baseline verification report](../reports/backbone_v2_baseline.json)
records the initial import, repeat import, and database checks.

HPA prognosis records retain their cohort, direction and p-value as
direct disease evidence under the existing evidence policy. The progression
edges express the project stage order and use contextual background.
The provided v2 counts are comparison values. The import follows the
available source records and reports the difference.

## Offline v3 backbone

The local v3 build combines the curated v2 TSVs, archived pathway records and
the original HMDB XML. See the [v3 schema](../docs/backbone_v3_schema.md) for
the build and restore commands, model, and literature loader.

## v2 Primary Scripts

| Script | Role | Writes to Neo4j |
|---|---|---|
| `crawl_disgenet_liver_backbone.py` | Crawls the five-disease DisGeNET backbone into raw/staging/curated/tiered files. | No |
| `import_disgenet_backbone_neo4j.py` | Imports curated DisGeNET Disease, Gene, and Gene-Disease association tables. | Yes |
| `import_string_neo4j.py` | Maps Gene to STRING Protein and imports high-confidence PPI. | Yes with `--apply` |
| `import_kegg_neo4j.py` | Imports KEGG human Gene-Pathway memberships. | Yes with `--apply` |
| `import_reactome_static_neo4j.py` | Imports Reactome memberships from the local static Ensembl2Reactome file. | Yes with `--apply` |
| `import_hpa_neo4j.py` | Imports HPA liver tissue expression and LIHC prognostic relations. | Yes with `--apply` |
| `import_hmdb_neo4j.py` | Imports conservative HMDB disease-linked metabolite context. | Yes unless `--build-only` |

## Legacy or Supporting Scripts

| Script | Status |
|---|---|
| `extract_disgenet_hcc_api.py` | Older/smoke DisGeNET extractor; uses `top-n` sampling, not suitable for the final v2 full crawl. |
| `import_reactome_neo4j.py` | API-based Reactome importer; can be slow on the v2 836-gene set. Prefer `import_reactome_static_neo4j.py` for v2. |
| `export_disgenet_neo4j.py` | Exports older normalized DisGeNET JSONL into Neo4j CSV files. |
| `export_neo4j_ontology_schema.py` | Exports ontology/schema metadata Cypher. |
| `audit_disgenet_api_fields.py` | Audits DisGeNET API field shapes. |
| `validate_extracted_data.py` | Validates older extracted data files against ontology rules. |

## Safety Rules

- Use `NEO4J_PASSWORD` as an environment variable; do not write credentials into
  scripts or reports.
- Check the target `--neo4j-url` before running any import. v2 uses:

```text
http://100.104.181.96:7474/db/liver-kg-core-v02/tx/commit
```

- Scripts use `MERGE` for core IDs, so reruns are intended to be mostly
  idempotent. They may still update properties such as `updated_at`.
