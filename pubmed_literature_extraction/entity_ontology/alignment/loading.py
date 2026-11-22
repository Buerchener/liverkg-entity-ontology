"""Read existing ontology artifacts and Neo4j entities without changing either.

Only fixed MATCH/RETURN queries are used. Neo4j sessions and transactions request
READ_ACCESS. Credentials are arguments supplied by the CLI from the environment,
never included in snapshots. Element IDs identify nodes in the named database at
snapshot time; original application identifiers remain in source.properties.
"""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from ..entity_types import IN_SCOPE
from ..storage import read_json
from .candidates import canonical_standard_id


TYPE_ALIASES = {name.casefold(): name for name in IN_SCOPE}
TYPE_ALIASES.update({"cell_type": "CellType", "cell type": "CellType"})
TYPE_ID_FIELDS = {
    "Disease": "disease_id", "Gene": "gene_id", "Protein": "protein_id",
    "Pathway": "pathway_id", "Metabolite": "metabolite_id",
    "Tissue": "tissue_id", "CellType": "cell_type_id",
}
NAMESPACES = {
    "ncbi gene": "NCBIGene", "ncbigene": "NCBIGene", "entrez": "NCBIGene",
    "entrezgene": "NCBIGene", "hgnc": "HGNC", "ensembl": "Ensembl",
    "uniprot": "UniProt", "uniprotkb": "UniProt", "string": "STRING",
    "kegg gene": "KEGG_GENE", "kegg_gene": "KEGG_GENE",
    "kegg pathway": "KEGG_PATHWAY", "kegg_pathway": "KEGG_PATHWAY",
    "reactome": "REACTOME", "umls": "UMLS", "mesh": "MESH",
    "mondo": "MONDO", "do": "DOID", "doid": "DOID", "efo": "EFO",
    "nci": "NCIT", "ncit": "NCIT", "chebi": "CHEBI", "hmdb": "HMDB",
    "go": "GO", "pr": "PR", "cl": "CL", "uberon": "UBERON",
}
COUNTS_QUERY = "MATCH (n) RETURN labels(n) AS labels, count(*) AS count ORDER BY labels"
SCHEMA_QUERY = (
    "MATCH (n) UNWIND keys(n) AS key "
    "RETURN labels(n) AS labels, collect(DISTINCT key) AS property_keys ORDER BY labels"
)
RELATION_COUNT_QUERY = "MATCH ()-[r]->() RETURN count(r) AS count"
SAMPLE_QUERY = (
    "MATCH (n) WHERE $kind IN labels(n) "
    "RETURN elementId(n) AS element_id, labels(n) AS labels, properties(n) AS properties "
    "ORDER BY coalesce(n.project_id, n.disease_id, n.gene_id, n.protein_id, "
    "n.pathway_id, n.metabolite_id, n.tissue_id, n.cell_type_id, n.name, ''), element_id "
    "LIMIT $limit"
)


def _json_safe(value):
    """Neo4j temporal values must remain readable in a portable JSON snapshot."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "iso_format"):
        return value.iso_format()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _strings(value, *, split=False):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if isinstance(v, (str, int)) and str(v).strip()]
    if isinstance(value, (str, int)) and str(value).strip():
        return [x.strip() for x in str(value).split(";")] if split else [str(value).strip()]
    return []


def _json_property(value):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            pass
    return None


def _kind(value):
    if not isinstance(value, str) or value.casefold() not in TYPE_ALIASES:
        raise ValueError("Unsupported or missing entity type: %r" % value)
    return TYPE_ALIASES[value.casefold()]


def _qualified_id(value, namespace=None, *, kind=None):
    value = str(value).strip()
    if not value or value.upper().startswith(("MENTION:", "PROJECT:", "PMID:")):
        return None
    if value.startswith("http"):
        match = re.search(r"/obo/([A-Za-z]+)_(\w+)$", value)
        if not match:
            return None
        namespace, value = match.groups()
    # Imported DisGeNET xrefs use e.g. MONDO_0013209 and MESH_D065626.
    match = re.fullmatch(r"([A-Za-z][A-Za-z_ ]*)[:_]([A-Za-z0-9][A-Za-z0-9_.:-]*)", value)
    if match:
        prefix, local = match.groups()
        canonical = NAMESPACES.get(prefix.casefold())
        if prefix.casefold() == "kegg" and kind == "Pathway":
            canonical = "KEGG_PATHWAY"
        if canonical:
            namespace, value = canonical, local
        elif prefix.casefold() == "path":
            namespace, value = "KEGG_PATHWAY", local
        elif namespace is None:
            return None
    namespace = NAMESPACES.get(str(namespace).casefold(), namespace)
    if namespace not in set(NAMESPACES.values()):
        return None
    if namespace == "KEGG_PATHWAY" and value.startswith("path:"):
        value = value[5:]
    return f"{namespace}:{value}"


def extract_standard_ids(properties, kind):
    """Use namespace-bearing identifiers; never guess an unqualified numeric ID.

    Gene protein xrefs are deliberately excluded: a gene and its product are
    different entities even when a source attaches both IDs to one record.
    """
    result = set()

    def add(value, namespace=None):
        for item in _strings(value, split=True):
            ident = _qualified_id(item, namespace, kind=kind)
            if ident:
                result.add(ident)

    for key in ("standard_ids", "primary_external_id", "project_id", TYPE_ID_FIELDS[kind]):
        add(properties.get(key))
    for key in ("external_ids_json", "external_ids", "xrefs"):
        raw = properties.get(key)
        decoded = _json_property(raw)
        if isinstance(decoded, dict):
            for namespace, values in decoded.items():
                canonical = NAMESPACES.get(namespace.casefold())
                if canonical and not (kind == "Gene" and canonical in {"UniProt", "STRING", "PR"}):
                    add(values, canonical)
        else:
            add(decoded if isinstance(decoded, list) else raw)
    direct = {
        "umls_id": "UMLS", "ncbi_gene_id": "NCBIGene", "hgnc_id": "HGNC",
        "ensembl_gene_ids": "Ensembl", "uniprot_id": "UniProt",
        "string_protein_id": "STRING", "kegg_pathway_id": "KEGG_PATHWAY",
        "reactome_id": "REACTOME", "chebi_id": "CHEBI", "hmdb_id": "HMDB",
        "go_id": "GO", "mondo_id": "MONDO", "doid": "DOID", "pr_id": "PR",
        "uberon_id": "UBERON", "cl_id": "CL",
    }
    for key, namespace in direct.items():
        if kind == "Gene" and namespace in {"UniProt", "STRING"}:
            continue
        add(properties.get(key), namespace)
    if kind == "Protein" and re.fullmatch(r"\d+\.ENSP\d+", str(properties.get("protein_id", ""))):
        add(properties["protein_id"], "STRING")
    if kind == "Gene":
        result = {x for x in result if x.split(":", 1)[0] not in {"UniProt", "STRING", "PR"}}
    return sorted({canonical for value in result if (canonical := canonical_standard_id(value))})


def _source_file(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def load_ontology(path, equivalence_path=None):
    """Load placed graph nodes only, retaining existing IS_A edges unchanged."""
    graph = read_json(path)
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        raise ValueError("Ontology input must be placement JSON with nodes and edges")
    supplemental = {}
    if equivalence_path:
        supplemental = {row["id"]: row for row in read_json(equivalence_path).get("concepts", [])}
    nodes, by_id = [], {}
    for original in graph["nodes"]:
        row = {**supplemental.get(original.get("id"), {}), **original}
        ident = row.get("id")
        if not isinstance(ident, str) or not ident or ident in by_id:
            raise ValueError("Ontology node IDs must be nonempty and unique")
        if not isinstance(row.get("name"), str) or not row["name"].strip():
            raise ValueError("Ontology node names must be nonempty")
        kind = _kind(row.get("kind", row.get("type")))
        node = {
            **row, "id": ident, "kind": kind, "definition": str(row.get("definition") or ""),
            "aliases": sorted(set(_strings(row.get("aliases")) + _strings(supplemental.get(ident, {}).get("aliases")))),
            "standard_ids": extract_standard_ids(row, kind), "parent_ids": [],
            "is_root": row.get("origin") == "schema_root" or ident.startswith("type:"),
            "source": {"artifact": str(Path(path).resolve()), "original_node": original},
        }
        explicit_taxon = row.get("taxon_id", row.get("ncbi_taxon_id"))
        if explicit_taxon is not None:
            node["taxon_id"] = str(explicit_taxon)
        explicit_species = row.get("species") or row.get("species_name") or row.get("organism")
        if explicit_species:
            node["species"] = explicit_species
        by_id[ident] = node
        nodes.append(node)
    edges = graph.get("edges", [])
    if not isinstance(edges, list):
        raise ValueError("Ontology edges must be a list")
    for edge in edges:
        if str(edge.get("relation", "is_a")).casefold() != "is_a":
            continue
        child, parent = edge.get("child_id"), edge.get("parent_id")
        if child not in by_id or parent not in by_id:
            raise ValueError("IS_A edge refers to a node absent from the existing graph")
        by_id[child]["parent_ids"].append(parent)
    for node in nodes:
        node["parent_ids"] = sorted(set(node["parent_ids"]))
    metadata = {
        "source": "ontology_placement", "source_file": _source_file(path),
        "node_count": len(nodes), "root_count": sum(n["is_root"] for n in nodes),
        "counts_by_type": dict(Counter(n["kind"] for n in nodes)),
        "edges": edges, "edges_are_provisional": graph.get("edges_are_provisional"),
        "source_concepts_are_not_added": True,
    }
    if equivalence_path:
        metadata["equivalence_file"] = _source_file(equivalence_path)
    return {"nodes": nodes, "metadata": metadata}


def normalize_kg_node(record, *, database=None):
    properties = _json_safe(record.get("properties", {}))
    labels = record.get("labels", [])
    kinds = sorted({_kind(label) for label in labels if str(label).casefold() in TYPE_ALIASES})
    if len(kinds) != 1:
        raise ValueError("A KG node must have exactly one supported entity type")
    kind = kinds[0]
    ident = record.get("element_id")
    if not isinstance(ident, str) or not ident:
        raise ValueError("KG records must include a Neo4j element_id")
    name_keys = ["name", "gene_symbol", "preferred_name", "disease_name", "gene_name", "pathway_name", "metabolite_name", "tissue_name", "cell_type_name"]
    names = [properties[k].strip() for k in name_keys if isinstance(properties.get(k), str) and properties[k].strip()]
    if not names:
        raise ValueError("KG node is missing a usable entity name: " + ident)
    attributes = _json_property(properties.get("attributes_json")) or {}
    if not isinstance(attributes, dict):
        attributes = {}
    descriptions = [properties.get(k) for k in ("definition", "description", "annotation")]
    descriptions += [attributes.get("description")]
    definition = "\n".join(dict.fromkeys(x.strip() for x in descriptions if isinstance(x, str) and x.strip()))
    aliases = set(names[1:] + _strings(properties.get("aliases")) + _strings(properties.get("synonyms"), split=True))
    aliases.discard(names[0])
    level, level_evidence = "unknown", []
    explicit = properties.get("entity_level")
    if explicit in {"class", "individual", "unknown"}:
        level, level_evidence = explicit, [{"property": "entity_level", "value": explicit}]
    elif isinstance(properties.get("is_class"), bool):
        level = "class" if properties["is_class"] else "unknown"
        level_evidence = [{"property": "is_class", "value": properties["is_class"]}]
    species = properties.get("species_name") or properties.get("species") or properties.get("organism") or attributes.get("organism")
    source_context = {key: properties[key] for key in (
        "source", "evidence", "source_record_id", "disease_type", "gene_type",
        "protein_class_names", "pathway_category", "class", "super_class", "kingdom",
    ) if properties.get(key) not in (None, "", [])}
    taxon = properties.get("ncbi_taxon_id", properties.get("taxon_id"))
    return {
        "id": ident, "kg_node_id": ident, "name": names[0], "kind": kind,
        "definition": definition, "aliases": sorted(aliases),
        "standard_ids": extract_standard_ids(properties, kind),
        "parent_ids": [], "is_root": False,
        "entity_level": level, "entity_level_evidence": level_evidence,
        "species": species, "ncbi_taxon_id": properties.get("ncbi_taxon_id"),
        "taxon_id": str(taxon) if taxon is not None else None, "source_context": source_context,
        "source": {"database": database, "element_id": ident, "labels": labels, "properties": properties},
    }


def _inspect_session(session):
    counts = session.execute_read(lambda tx: tx.run(COUNTS_QUERY).data())
    schema = session.execute_read(lambda tx: tx.run(SCHEMA_QUERY).data())
    relationship_count = session.execute_read(lambda tx: tx.run(RELATION_COUNT_QUERY).single()["count"])
    per_type = {kind: sum(row["count"] for row in counts if kind in row["labels"]) for kind in IN_SCOPE}
    return {"label_counts": counts, "property_schema": schema, "counts_by_type": per_type,
            "total_nodes": sum(row["count"] for row in counts), "relationship_count": relationship_count}


def _driver(uri, username, password):
    try:
        from neo4j import GraphDatabase
    except ImportError:
        raise RuntimeError("Install the Neo4j driver: pip install -r entity_ontology/alignment/requirements.txt") from None
    parsed_uri = urlsplit(uri)
    if parsed_uri.username is not None or parsed_uri.password is not None:
        raise ValueError("Use separate Neo4j credentials, never credentials embedded in the URI")
    if not password:
        raise ValueError("Neo4j password environment variable is not set")
    return GraphDatabase.driver(uri, auth=(username, password), connection_timeout=15,
                                connection_acquisition_timeout=30, max_transaction_retry_time=10)


def inspect_neo4j(uri, username, password, *, database="neo4j"):
    from neo4j import READ_ACCESS
    with _driver(uri, username, password) as driver:
        with driver.session(database=database, default_access_mode=READ_ACCESS) as session:
            return _inspect_session(session)


def read_neo4j(uri, username, password, *, database="neo4j", limit_per_type=5, types=None):
    """Deterministic bounded sample per type, with population counts and schema."""
    from neo4j import READ_ACCESS
    if type(limit_per_type) is not int or not 1 <= limit_per_type <= 100000:
        raise ValueError("limit_per_type must be an integer from 1 to 100000")
    kinds = list(dict.fromkeys(_kind(x) for x in (types if types is not None else IN_SCOPE)))
    if not kinds:
        raise ValueError("At least one entity type must be requested")
    with _driver(uri, username, password) as driver:
        with driver.session(database=database, default_access_mode=READ_ACCESS) as session:
            metadata = _inspect_session(session)
            nodes = []
            for kind in kinds:
                rows = session.execute_read(lambda tx, kind=kind: tx.run(SAMPLE_QUERY, kind=kind, limit=limit_per_type).data())
                nodes.extend(normalize_kg_node(row, database=database) for row in rows)
    if len({node["id"] for node in nodes}) != len(nodes):
        raise ValueError("Duplicate KG node IDs after type sampling")
    metadata.update({"source": "neo4j", "uri": uri, "database": database,
                     "access_mode": "READ", "snapshot_time_utc": datetime.now(timezone.utc).isoformat(),
                     "sample_strategy": "sorted application identifier then elementId, per entity type",
                     "limit_per_type": limit_per_type, "requested_types": kinds,
                     "sample_counts_by_type": dict(Counter(n["kind"] for n in nodes)),
                     "sample_size": len(nodes), "element_ids_are_database_scoped": True})
    return {"nodes": nodes, "metadata": metadata}


def load_kg_snapshot(path):
    """Validate normalized nodes before retrieval, preserving original evidence.

    Missing optional text/list fields receive empty defaults. Supplied values
    must have their documented types; malformed values are not silently coerced.
    """
    snapshot = read_json(path)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("nodes"), list):
        raise ValueError("KG snapshot must contain nodes and metadata")
    if not isinstance(snapshot.get("metadata", {}), dict):
        raise ValueError("KG snapshot metadata must be an object")
    seen = set()
    nodes = []
    for node in snapshot["nodes"]:
        if (not isinstance(node, dict) or not isinstance(node.get("id"), str)
                or not node["id"].strip() or node["id"] in seen):
            raise ValueError("KG snapshot node IDs must be nonempty and unique")
        seen.add(node["id"])
        normalized = {**node, "kind": _kind(node.get("kind"))}
        if not isinstance(node.get("name"), str) or not node["name"].strip():
            raise ValueError("KG snapshot node names must be nonempty")
        if not isinstance(node.get("definition", ""), str):
            raise ValueError("KG snapshot definition must be a string")
        normalized.setdefault("definition", "")
        for field in ("aliases", "standard_ids", "parent_ids"):
            values = node.get(field, [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError("KG snapshot %s must be a list of strings" % field)
            normalized.setdefault(field, [])
        level = node.get("entity_level", "unknown")
        if not isinstance(level, str) or level not in {"class", "individual", "unknown"}:
            raise ValueError("KG snapshot entity_level must be class, individual, or unknown")
        normalized.setdefault("entity_level", "unknown")
        nodes.append(normalized)
    return {"nodes": nodes, "metadata": {**snapshot.get("metadata", {}), "snapshot_file": _source_file(path)}}
