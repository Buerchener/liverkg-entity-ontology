"""Rebuild the curated v2 backbone from the same frozen inputs used for linking."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import csv
import hashlib
from io import StringIO
from itertools import batched, pairwise
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from cognitive_agent.schema.write_contract import V2_MAIN_KG_WRITE_CONTRACT

from .backbone_schema import ROOT_CLASS_ID
from .export.neo4j_load import BATCH_SIZE, check_target
from .linking.kg_links import V2_FILES, V2_NODE_KEYS, gene_key, hmdb_key, resolve_v2_data_dir


VERSION = "v2"
DIRECT = "direct_disease_evidence"
CONTEXT = "contextual_background"
REFERENCE_NODES = {
    "Disease": 5, "Gene": 836, "Protein": 793, "Pathway": 1721,
    "Tissue": 1, "CellType": 154, "Metabolite": 36,
}
REFERENCE_RELATIONSHIPS = {
    "ASSOCIATED_WITH": 1079, "PROGRESSES_TO": 4, "ENCODES": 793,
    "INTERACTS_WITH": 7154, "PARTICIPATES_IN": 9947, "EXPRESSED_IN": 17745,
    "PROGNOSTIC_IN": 1384, "ASSOCIATED_WITH_METABOLITE": 97,
}
NODE_TABLES = {
    "diseases": "Disease", "genes": "Gene", "string_mapping": "Protein",
    "kegg_pathways": "Pathway", "reactome_pathways": "Pathway",
    "tissues": "Tissue", "cell_types": "CellType", "metabolites": "Metabolite",
}
# Table -> (predicate, endpoint labels), endpoint columns, evidence class.
RELATION_TABLES = {
    "gene_disease": (("ASSOCIATED_WITH", "Gene", "Disease"), "gene_id", "disease_id", DIRECT),
    "string_interactions": (
        ("INTERACTS_WITH", "Protein", "Protein"), "string_protein_id_a", "string_protein_id_b", CONTEXT
    ),
    "kegg_members": (("PARTICIPATES_IN", "Gene", "Pathway"), "gene_id", "pathway_id", CONTEXT),
    "reactome_members": (("PARTICIPATES_IN", "Gene", "Pathway"), "gene_id", "pathway_id", CONTEXT),
    "tissue_expression": (("EXPRESSED_IN", "Gene", "Tissue"), "gene_id", "tissue_id", CONTEXT),
    "cell_expression": (("EXPRESSED_IN", "Gene", "CellType"), "gene_id", "cell_type_id", CONTEXT),
    "lihc_prognostic": (("PROGNOSTIC_IN", "Gene", "Disease"), "gene_id", "disease_id", DIRECT),
    "gene_metabolite": (("ASSOCIATED_WITH_METABOLITE", "Gene", "Metabolite"), "gene_id", "hmdb_id", CONTEXT),
    "disease_metabolite": (("ASSOCIATED_WITH", "Metabolite", "Disease"), "hmdb_id", "disease_id", CONTEXT),
}
INT_FIELDS = {
    "stage_order", "ncbi_taxon_id", "required_score", "year_initial", "year_final",
    "num_pmids", "num_db_snp", "num_clinical_trials", "num_chemicals",
    "num_pmids_with_chemicals", "num_trials_with_chemicals",
}
FLOAT_FIELDS = {
    "gene_dsi", "gene_dpi", "gene_pli", "score", "normalized_score", "evidence_index",
    "nscore", "fscore", "pscore", "ascore", "escore", "dscore", "tscore",
    "n_tpm", "n_cpm", "p_value", "monoisotopic_molecular_weight", "average_molecular_weight",
}
SUMMARY_QUERIES = {
    "nodes_by_label": "MATCH (n) UNWIND labels(n) AS kind RETURN kind, count(*) AS count ORDER BY kind",
    "nodes_by_source": "MATCH (n) RETURN n.source AS kind, count(*) AS count ORDER BY kind",
    "relationships_by_type": "MATCH ()-[r]->() RETURN type(r) AS kind, count(*) AS count ORDER BY kind",
    "relationships_by_source": "MATCH ()-[r]->() RETURN r.source AS kind, count(*) AS count ORDER BY kind",
    "relationships_by_evidence": (
        "MATCH ()-[r]->() RETURN r.evidence_class AS kind, count(*) AS count ORDER BY kind"
    ),
}


V3_SUMMARY_QUERIES = {
    name: query.replace("RETURN", "WHERE n.backbone_version = $version RETURN", 1)
    if name == "nodes_by_source"
    else query.replace(" UNWIND", " WHERE n.backbone_version = $version UNWIND", 1)
    if name == "nodes_by_label"
    else query.replace("RETURN", "WHERE r.backbone_version = $version RETURN", 1)
    for name, query in SUMMARY_QUERIES.items()
}

type Properties = dict[str, str | int | float | bool | list[str] | list[int]]
type NodeRef = tuple[str, str]
type Signature = tuple[str, str, str]


@dataclass
class Table:
    path: str
    sha256: str
    rows: list[dict[str, str]]


@dataclass
class Relationship:
    start: NodeRef
    end: NodeRef
    properties: Properties


@dataclass
class BackbonePlan:
    tables: dict[str, Table]
    version: str = VERSION
    node_keys: dict[str, str] = field(default_factory=lambda: V2_NODE_KEYS)
    relationship_keys: dict[Signature, str] = field(default_factory=lambda: V2_MAIN_KG_WRITE_CONTRACT)
    nodes: dict[NodeRef, Properties] = field(default_factory=dict)
    relationships: dict[tuple[str, str, str], Relationship] = field(default_factory=dict)
    unmapped_string_genes: int = 0

    def add_node(self, label: str, properties: Properties) -> None:
        ref = (label, _required(properties, self.node_keys[label]))
        if self.nodes.setdefault(ref, properties) != properties:
            raise ValueError(f"Conflicting node records for {ref[0]} {ref[1]}.")

    def add_relationship(self, signature: Signature, start_id: str, end_id: str, properties: Properties) -> None:
        predicate, start_label, end_label = signature
        id_property = self.relationship_keys[signature]
        record_id = _required(properties, id_property)
        start, end = (start_label, start_id), (end_label, end_id)
        for label, identifier in (start, end):
            if (label, identifier) not in self.nodes:
                raise ValueError(f"Unknown {label} {identifier!r} in {predicate} record {record_id!r}.")
        record = Relationship(start, end, properties)
        key = (predicate, id_property, record_id)
        if self.relationships.setdefault(key, record) != record:
            raise ValueError(f"Conflicting {predicate} records for {record_id!r}.")

    def report(self) -> dict:
        node_counts = dict(Counter(kind for kind, _ in self.nodes))
        edge_counts = dict(Counter(kind for kind, _, _ in self.relationships))
        plan = _with_totals({
            "nodes_by_label": node_counts,
            "nodes_by_source": dict(Counter(row["source"] for row in self.nodes.values())),
            "relationships_by_type": edge_counts,
            "relationships_by_source": dict(Counter(
                edge.properties["source"] for edge in self.relationships.values()
            )),
            "relationships_by_evidence": dict(Counter(
                edge.properties["evidence_class"] for edge in self.relationships.values()
            )),
        })
        return {
            "backbone_version": self.version,
            "inputs": {
                name: {"path": table.path, "sha256": table.sha256, "rows": len(table.rows)}
                for name, table in sorted(self.tables.items())
            },
            "unmapped_string_genes": self.unmapped_string_genes,
            "reference": {
                "nodes_by_label": REFERENCE_NODES, "relationships_by_type": REFERENCE_RELATIONSHIPS,
                "node_count": sum(REFERENCE_NODES.values()),
                "relationship_count": sum(REFERENCE_RELATIONSHIPS.values()),
            },
            "plan": plan,
            "plan_delta_from_reference": {
                "nodes_by_label": _compare(node_counts, REFERENCE_NODES),
                "relationships_by_type": _compare(edge_counts, REFERENCE_RELATIONSHIPS),
            },
        }


def _compare(actual, reference):
    return {key: actual.get(key, 0) - reference.get(key, 0) for key in sorted(actual.keys() | reference.keys())}


def _with_totals(counts):
    return {
        "node_count": sum(counts["nodes_by_label"].values()),
        "relationship_count": sum(counts["relationships_by_type"].values()),
        **{group: dict(sorted(values.items())) for group, values in counts.items()},
    }


def _read_table(root: Path, path: str) -> Table:
    source = (root / path).read_bytes()
    stream = StringIO(source.decode("utf-8-sig"), newline="")
    reader = csv.DictReader(stream, delimiter="\t", strict=True)
    rows = []
    try:
        columns = reader.fieldnames
        if not columns or "" in columns or len(set(columns)) != len(columns):
            raise ValueError(f"Missing or duplicate TSV columns in {path}.")
        for row in reader:
            if None in row or None in row.values():
                raise ValueError(f"Unexpected TSV field count in {path}, line {reader.line_num}.")
            rows.append(row)
    except csv.Error as error:
        raise ValueError(f"Invalid TSV in {path}, line {reader.line_num}: {error}") from error
    return Table(path, hashlib.sha256(source).hexdigest(), rows)


def _required(row: dict, field_name: str) -> str:
    value = str(row.get(field_name, "")).strip()
    if not value:
        raise ValueError(f"Missing required property {field_name!r}.")
    return value


def _properties(row: dict[str, str], table: Table, *, source: str = "") -> Properties:
    properties: Properties = {}
    for key, value in row.items():
        if not value or not value.strip():
            continue
        if key in INT_FIELDS:
            properties[key] = int(value)
        elif key in FLOAT_FIELDS:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"Non-finite {key!r} in {table.path}.")
            properties[key] = number
        else:
            properties[key] = value
    properties.update(
        source=source or _required(row, "source"),
        source_version=f"v2:sha256:{table.sha256}",
        backbone_version=VERSION,
    )
    return properties


def _endpoint(label: str, identifier: str) -> str:
    if label == "Gene":
        return gene_key(identifier)
    if label == "Metabolite":
        return hmdb_key(identifier)
    return identifier


def _node_properties(label: str, row: dict[str, str], table: Table) -> Properties:
    if label == "Protein":
        protein_id = _required(row, "string_protein_id")
        fields = ("annotation", "ncbi_taxon_id", "preferred_name")
        props = _properties({key: row[key] for key in fields if key in row}, table, source="STRING")
        props.update(protein_id=protein_id, name=row.get("preferred_name") or protein_id, species_name="Homo sapiens")
        return props

    props = _properties(row, table)
    if label == "Disease":
        props["name"] = _required(row, "stage_code")
        if row.get("disease_vocabularies"):
            props["external_ids"] = row["disease_vocabularies"]
    elif label == "Gene":
        gene_id = _required(row, "gene_id")
        props.update(gene_id=gene_key(gene_id), ncbi_gene_id=gene_id, name=_required(row, "gene_symbol"))
        for raw, canonical in (
            ("gene_ncbi_type", "gene_type"), ("gene_ensembl_ids", "ensembl_gene_ids"),
            ("gene_protein_class_names", "protein_class_names"),
        ):
            if row.get(raw):
                props[canonical] = row[raw]
    elif label == "Pathway" and row.get("organism") == "hsa":
        props.update(ncbi_taxon_id=9606, species_name="Homo sapiens")
    return props


def _add_progression(plan: BackbonePlan) -> None:
    stages = {}
    for (label, disease_id), props in plan.nodes.items():
        if label == "Disease":
            order = props.get("stage_order")
            if not isinstance(order, int) or order < 1 or order in stages:
                raise ValueError(f"Invalid or duplicate stage_order on {disease_id!r}.")
            stages[order] = disease_id
    if len(stages) < 2:
        raise ValueError("The disease TSV must provide at least two ordered stages.")
    for (start_order, start), (end_order, end) in pairwise(sorted(stages.items())):
        props = _properties({}, plan.tables["diseases"], source="LiverTrail")
        props.update(
            progression_id=f"LIVERTRAIL_PROGRESSION:{start}|{end}",
            from_stage=start_order, to_stage=end_order, evidence_class=CONTEXT,
        )
        plan.add_relationship(("PROGRESSES_TO", "Disease", "Disease"), start, end, props)


def build_v2_plan(data_dir: Path | None = None) -> BackbonePlan:
    """Read and fully validate the frozen inputs before opening any database."""
    root = resolve_v2_data_dir(data_dir)
    plan = BackbonePlan({name: _read_table(root, file) for name, file in V2_FILES.items()})
    for name, label in NODE_TABLES.items():
        table = plan.tables[name]
        for row in table.rows:
            if label == "Protein":
                if _required(row, "mapping_status") != "mapped":
                    if row.get("string_protein_id"):
                        raise ValueError(f"Inconsistent STRING mapping in {table.path}.")
                    plan.unmapped_string_genes += 1
                    continue
            plan.add_node(label, _node_properties(label, row, table))

    table = plan.tables["string_mapping"]
    for row in table.rows:
        if row["mapping_status"] == "mapped":
            start, end = gene_key(_required(row, "gene_id")), _required(row, "string_protein_id")
            attrs = _properties(row, table, source="STRING_mapping")
            attrs.update(relationship_id=f"STRING_ENCODES:{start}|{end}", evidence_class=CONTEXT)
            plan.add_relationship(("ENCODES", "Gene", "Protein"), start, end, attrs)

    for name, (signature, start_column, end_column, evidence) in RELATION_TABLES.items():
        table = plan.tables[name]
        for row in table.rows:
            attrs = _properties(row, table)
            attrs["evidence_class"] = evidence
            start = _endpoint(signature[1], _required(row, start_column))
            end = _endpoint(signature[2], _required(row, end_column))
            plan.add_relationship(signature, start, end, attrs)
    _add_progression(plan)
    return plan


def _require_baseline_database(tx):
    for pattern, alias in (("(n)", "n"), ("()-[r]->()", "r")):
        query = f"MATCH {pattern} WHERE {alias}.backbone_version IS NULL OR {alias}.backbone_version <> $version"
        if tx.run(f"{query} RETURN 1 AS found LIMIT 1", version=VERSION).single() is not None:
            raise ValueError("Use an empty local database or the database already built by this v2 importer.")


def _require_target_database(tx, version, snapshot):
    if version == VERSION:
        return _require_baseline_database(tx)
    result = tx.run(
        "MATCH (n:BackboneClass {class_id: $class_id}) RETURN n.snapshot_id AS id",
        class_id=ROOT_CLASS_ID,
    ).single()
    if result is not None:
        if result["id"] != snapshot:
            raise ValueError("Use an empty database to build a different v3 snapshot.")
        return
    if tx.run("MATCH (n) RETURN 1 AS found LIMIT 1").single() is not None:
        raise ValueError("Use an empty local database for the first v3 import.")


def _constraints(plan):
    for label, key in plan.node_keys.items():
        yield (
            f"CREATE CONSTRAINT livertrail_backbone_{label.lower()}_{key} IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.{key} IS UNIQUE"
        )
    for predicate, key in sorted({(predicate, key) for (predicate, _, _), key in plan.relationship_keys.items()}):
        yield (
            f"CREATE CONSTRAINT livertrail_backbone_{predicate.lower()}_{key} IF NOT EXISTS "
            f"FOR ()-[r:{predicate}]-() REQUIRE r.{key} IS UNIQUE"
        )


def _update_properties(alias):
    return f"""
FOREACH (_ IN CASE WHEN {alias}.updated_at IS NULL OR
    any(k IN keys(row.properties) WHERE {alias}[k] IS NULL OR {alias}[k] <> row.properties[k])
    THEN [1] ELSE [] END |
    SET {alias} += row.properties, {alias}.updated_at = datetime()
)
RETURN count({alias}) AS written
"""


def _merge_rows(tx, query, rows, counters):
    for batch in batched(rows, BATCH_SIZE):
        result = tx.run(query, rows=list(batch))
        if result.single(strict=True)["written"] != len(batch):
            raise ValueError("A Neo4j batch did not resolve every planned record. The import was rolled back.")
        outcome = result.consume().counters
        counters["nodes_created"] += outcome.nodes_created
        counters["relationships_created"] += outcome.relationships_created
        counters["properties_set"] += outcome.properties_set


def _write_graph(tx, plan, snapshot):
    _require_target_database(tx, plan.version, snapshot)
    counters = dict(nodes_created=0, relationships_created=0, properties_set=0)
    node_groups = defaultdict(list)
    for (label, node_id), properties in plan.nodes.items():
        if snapshot and (label, node_id) == ("BackboneClass", ROOT_CLASS_ID):
            properties = {**properties, "snapshot_id": snapshot}
        node_groups[label].append({"node_id": node_id, "properties": properties})
    for label, rows in node_groups.items():
        stage = ""
        if snapshot and label == "Disease":
            stage = "FOREACH (_ IN CASE WHEN row.properties.stage_order IS NOT NULL THEN [1] ELSE [] END | SET n:Stage)\n"
        query = (
            f"UNWIND $rows AS row\nMERGE (n:{label} {{{plan.node_keys[label]}: row.node_id}})\n"
            + stage + _update_properties("n")
        )
        _merge_rows(tx, query, rows, counters)

    edge_groups = defaultdict(list)
    for (predicate, key, record_id), edge in plan.relationships.items():
        group = predicate, key, edge.start[0], edge.end[0]
        edge_groups[group].append({
            "start_id": edge.start[1], "end_id": edge.end[1], "record_id": record_id, "properties": edge.properties,
        })
    for (predicate, key, start, end), rows in edge_groups.items():
        query = (
            "UNWIND $rows AS row\n"
            f"MATCH (a:{start} {{{plan.node_keys[start]}: row.start_id}})\n"
            f"MATCH (b:{end} {{{plan.node_keys[end]}: row.end_id}})\n"
            f"MERGE (a)-[r:{predicate} {{{key}: row.record_id}}]->(b)\n"
            + _update_properties("r")
        )
        _merge_rows(tx, query, rows, counters)

    queries = V3_SUMMARY_QUERIES if snapshot else SUMMARY_QUERIES
    counts = _with_totals({
        name: {record["kind"]: record["count"] for record in tx.run(query, version=plan.version)}
        for name, query in queries.items()
    })
    if snapshot:
        counts["node_count"] = tx.run(
            "MATCH (n) WHERE n.backbone_version = $version RETURN count(n) AS count", version=plan.version,
        ).single(strict=True)["count"]
    return {**counts, "writes": counters}


def apply_v2_plan(plan: BackbonePlan, *, uri: str, database: str | None) -> dict:
    return apply_plan(plan, uri=uri, database=database)


def apply_plan(plan: BackbonePlan, *, uri: str, database: str | None, snapshot=None) -> dict:
    """Apply the validated plan in one data transaction on a direct loopback Bolt connection."""
    if (plan.version != VERSION) != bool(snapshot):
        raise ValueError("An exported snapshot ID is required for v3.")
    check_target(uri, database)
    parsed = urlsplit(uri)
    if parsed.scheme != "bolt" or parsed.username is not None or parsed.password is not None or parsed.query:
        raise ValueError("Use a direct bolt:// loopback URI and pass credentials through environment variables.")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        raise ValueError("Set NEO4J_PASSWORD for the local Neo4j instance.")

    from neo4j import GraphDatabase
    from neo4j.exceptions import DriverError, Neo4jError

    try:
        with GraphDatabase.driver(uri, auth=(user, password), telemetry_disabled=True) as driver:
            with driver.session(database=database.strip()) as session:
                session.execute_read(_require_target_database, plan.version, snapshot)
                for statement in _constraints(plan):
                    session.run(statement).consume()
                result = session.execute_write(_write_graph, plan, snapshot)
    except (Neo4jError, DriverError) as error:
        code = getattr(error, "code", None) or type(error).__name__
        raise ValueError(f"Local Neo4j import failed ({code}).") from error
    return {"name": database.strip(), **result}
