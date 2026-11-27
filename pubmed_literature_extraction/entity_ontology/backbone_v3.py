"""Build the expanded LiverTrail backbone from frozen offline resources."""

from collections import Counter, defaultdict
import hashlib
from itertools import pairwise
from pathlib import Path

from cognitive_agent.schema.write_contract import METABOLITE_DISEASE

from .backbone import CONTEXT, build_v2_plan
from .backbone_hmdb import TAXONOMY_LEVELS, select_hmdb
from .backbone_pathways import KEGG_FILES, REACTOME_FILES, ancestors, read_pathways, read_source_manifest, source_version
from .backbone_schema import NODE_KEYS, ROOT_CLASS_ID, V3_NODE_KEYS, V3_RELATION_KEYS


VERSION = "v3"
RULE_VERSION = "LiverTrail v3 rules 1"
DEFAULT_SOURCES = Path(__file__).resolve().parents[2] / "data" / "backbone_v3_sources"
INFERRED = "inferred_or_hypothesis"
STAGE_ALIASES = {
    "NAFLD": (
        "MASLD", "non-alcoholic fatty liver disease", "nonalcoholic fatty liver disease",
        "metabolic dysfunction-associated steatotic liver disease",
    ),
    "NASH": (
        "MASH", "non-alcoholic steatohepatitis", "nonalcoholic steatohepatitis",
        "metabolic dysfunction-associated steatohepatitis",
    ),
    "Fibrosis": ("liver fibrosis", "hepatic fibrosis"),
    "Cirrhosis": ("liver cirrhosis", "hepatic cirrhosis"),
    "HCC": ("LIHC", "hepatocellular carcinoma", "liver hepatocellular carcinoma"),
}
HMDB_DISEASE_ALIASES = {"HCC": ("liver cancer", "liver carcinoma")}


def class_id(label):
    return f"LiverTrail:{label}"


def _meta(source="LiverTrail", version=RULE_VERSION):
    return {"source": source, "source_version": version, "backbone_version": VERSION}


def _edge(plan, kind, left, right, identifier, *, source="LiverTrail", version=RULE_VERSION,
          evidence=CONTEXT, properties=None):
    signature = (kind, left[0], right[0])
    attrs = {**_meta(source, version), "evidence_class": evidence, **(properties or {})}
    attrs[plan.relationship_keys[signature]] = identifier
    plan.add_relationship(signature, left[1], right[1], attrs)


def _class_node(plan, identifier, name, *, source="LiverTrail", version=RULE_VERSION, **props):
    plan.add_node("BackboneClass", {"class_id": identifier, "name": name, **_meta(source, version), **props})


def _is_a(plan, start, parent, *, source="LiverTrail", version=RULE_VERSION):
    _edge(plan, "IS_A", start, ("BackboneClass", parent),
          f"{start[0]}:{start[1]}|{parent}", source=source, version=version)


def _initialize(plan):
    plan.version = VERSION
    plan.node_keys = V3_NODE_KEYS
    plan.relationship_keys = V3_RELATION_KEYS
    for (label, identifier), props in plan.nodes.items():
        props["backbone_version"] = VERSION
        aliases = set()
        xrefs = {identifier}
        if label == "Disease":
            stage = props["stage_code"]
            aliases.update((*STAGE_ALIASES[stage], stage, props["disease_name"]))
            if stage == "NAFLD":
                props["related_aliases"] = ["MetALD", "Met-ALD"]
            for entry in props.get("disease_vocabularies", "").split(";"):
                prefix, sep, value = entry.partition("_")
                if sep:
                    xrefs.add(f"{'DOID' if prefix == 'DO' else prefix}:{value}")
        elif label == "Gene":
            xrefs.update(f"ENSEMBL:{value.strip()}" for value in props.get("gene_ensembl_ids", "").split(";")
                         if value.strip().startswith("ENSG"))
            aliases.update(value for value in (props.get("gene_symbol"), props.get("gene_name")) if value)
        elif label == "Protein":
            xrefs = {f"STRING:{identifier}"}
            ensembl = identifier.removeprefix("9606.")
            if ensembl.startswith("ENSP"):
                xrefs.add(f"ENSEMBL:{ensembl}")
        elif label == "Pathway":
            aliases.update(value for value in (props.get("pathway_name"),) if value)
        props.update(aliases=sorted(aliases), xrefs=sorted(xrefs))

    for key, edge in list(plan.relationships.items()):
        edge.properties["backbone_version"] = VERSION
        predicate, field, identifier = key
        if predicate == "ASSOCIATED_WITH" and edge.start[0] == "Metabolite":
            del plan.relationships[key]
            plan.relationships[(METABOLITE_DISEASE, field, identifier)] = edge

    _class_node(plan, ROOT_CLASS_ID, "LiverTrail Entity", level="root")
    for label in NODE_KEYS:
        _class_node(plan, class_id(label), label, level="entity_type", entity_label=label)
        _is_a(plan, ("BackboneClass", class_id(label)), ROOT_CLASS_ID)


def _add_pathways(plan, catalog, files):
    reactome_version = source_version(files[REACTOME_FILES[0]])
    reactome_hierarchy_version = source_version(files[REACTOME_FILES[1]])
    kegg_version = source_version(files[KEGG_FILES[0]])
    go_version = source_version(files[REACTOME_FILES[3]])
    for identifier, name in catalog.names.items():
        ref = ("Pathway", identifier)
        if ref in plan.nodes:
            aliases = set(plan.nodes[ref]["aliases"])
            if name != plan.nodes[ref]["name"]:
                aliases.add(name)
            plan.nodes[ref]["aliases"] = sorted(aliases)
            continue
        group = identifier in catalog.kegg_groups
        attrs = _meta("KEGG" if group else "Reactome", kegg_version if group else reactome_version)
        plan.add_node("Pathway", {
            **attrs, "pathway_id": identifier, "name": name,
            "pathway_group": group, "source_only_ancestor": True,
            "aliases": [], "xrefs": [] if group else [identifier],
        })
    for child, parents in catalog.parents.items():
        for parent in parents:
            kegg = child.startswith(("KEGG:", "KEGG-BRITE:"))
            _edge(plan, "SUBPATHWAY_OF", ("Pathway", child), ("Pathway", parent), f"{child}|{parent}",
                  source="KEGG" if kegg else "Reactome", version=kegg_version if kegg else reactome_hierarchy_version)

    for pathway, identifiers in catalog.go.items():
        plan.nodes["Pathway", pathway]["go_xrefs"] = sorted(identifiers)
        for go in identifiers:
            plan.add_node("ExternalReference", {"xref": go, "name": go, "namespace": "GO",
                                                **_meta("Reactome", go_version)})
            _edge(plan, "CROSS_REFERENCE", ("Pathway", pathway), ("ExternalReference", go),
                  f"{pathway}|{go}", source="Reactome", version=go_version,
                  properties={"scope": "go_biological_process_annotation"})


def _hmdb_association(plan, kind, start, end, identifier, version, attributes):
    key = (kind, "relationship_id", identifier)
    if old := plan.relationships.get(key):
        if old.start != start or old.end != end:
            raise ValueError(f"HMDB relationship ID {identifier!r} has competing endpoints.")
        old.properties.update(attributes, hmdb_source_version=version)
    else:
        _edge(plan, kind, start, end, identifier, source="HMDB", version=version, properties=attributes)


def _add_hmdb(plan, selection, source, files):
    hmdb_version = source_version(source)
    pathway_versions = {"HMDB": hmdb_version, "KEGG": source_version(files[KEGG_FILES[1]]),
                        "Reactome": source_version(files[REACTOME_FILES[2]])}
    for row in selection.records:
        ref = ("Metabolite", row.identifier)
        if ref in plan.nodes:
            plan.nodes[ref].update(row.properties, hmdb_source_version=hmdb_version)
        else:
            plan.add_node("Metabolite", {**_meta("HMDB", hmdb_version), **row.properties})
        accession = row.properties["hmdb_id"]

        parent = class_id("Metabolite")
        class_path = []
        for level in TAXONOMY_LEVELS:
            name = row.properties.get(level)
            if not name:
                continue
            class_path.append(f"{level}={name}")
            digest = hashlib.sha256("\0".join(class_path).encode()).hexdigest()[:24]
            child = f"HMDB:Class:{digest}"
            _class_node(plan, child, name, source="HMDB", version=hmdb_version, level=level)
            _is_a(plan, ("BackboneClass", child), parent, source="HMDB", version=hmdb_version)
            parent = child
        if class_path:
            _is_a(plan, ref, parent, source="HMDB", version=hmdb_version)

        for gene, attrs in row.genes.items():
            key = f"HMDB_GENE_METABOLITE:{gene.removeprefix('NCBIGene:')}|{accession}"
            _hmdb_association(plan, "ASSOCIATED_WITH_METABOLITE", ("Gene", gene), ref, key, hmdb_version, attrs)
        for disease, attrs in row.diseases.items():
            key = f"HMDB_DISEASE_METABOLITE:{disease}|{accession}"
            _hmdb_association(plan, METABOLITE_DISEASE, ref, ("Disease", disease), key, hmdb_version, attrs)
        for pathway, support in row.pathways.items():
            by_source = defaultdict(set)
            for mapping_source, xref in support:
                by_source[mapping_source].add(xref)
            for mapping_source, xrefs in by_source.items():
                _edge(plan, "PARTICIPATES_IN", ref, ("Pathway", pathway),
                      f"{mapping_source}_METABOLITE_PATHWAY:{row.identifier}|{pathway}",
                      source=mapping_source, version=pathway_versions[mapping_source],
                      properties={"mapping_ids": sorted(xrefs), "hmdb_xref_version": hmdb_version})


def _profile(counts):
    stages = [stage for stage, count in enumerate(counts, 1) if count]
    profile = {"stage_profile": counts, "stages": stages}
    if stages:
        profile.update(first_stage=stages[0], last_stage=stages[-1])
    return profile


def _gene_pattern(stages):
    if not stages:
        return "unmapped"
    if stages[-1] <= 2:
        return "early_only"
    if stages[0] >= 4:
        return "late_only"
    if stages == [1, 2, 3, 4, 5]:
        return "persistent"
    return "transition"


def _add_profiles(plan, parents):
    diseases = {node_id: props["stage_order"] for (label, node_id), props in plan.nodes.items() if label == "Disease"}
    if sorted(diseases.values()) != [1, 2, 3, 4, 5]:
        raise ValueError("The v3 progression backbone requires one disease for each stage 1 through 5.")
    gene_counts = defaultdict(Counter)
    metabolite_stages = defaultdict(set)
    pathway_genes = defaultdict(set)
    for (kind, _, _), edge in plan.relationships.items():
        if kind == "ASSOCIATED_WITH" and edge.start[0] == "Gene":
            gene_counts[edge.start[1]][diseases[edge.end[1]]] += 1
        elif kind == METABOLITE_DISEASE:
            metabolite_stages[edge.start[1]].add(diseases[edge.end[1]])
        elif kind == "PARTICIPATES_IN" and edge.start[0] == "Gene":
            pathway_genes[edge.end[1]].add(edge.start[1])

    ancestry = {pathway: ancestors([pathway], parents) for pathway in pathway_genes}
    aggregate_pathway_genes = defaultdict(set)
    for pathway, members in pathway_genes.items():
        for parent in ancestry[pathway]:
            aggregate_pathway_genes[parent].update(members)
    stages = dict(sorted((stage, disease) for disease, stage in diseases.items()))
    stage_genes = defaultdict(set)
    for (label, identifier), props in plan.nodes.items():
        if label == "Gene":
            counts = [gene_counts[identifier][stage] for stage in stages]
            profile = _profile(counts)
            props.update(profile, stage_pattern=_gene_pattern(profile["stages"]))
            for stage in profile["stages"]:
                stage_genes[stage].add(identifier)
        elif label == "Pathway":
            members = aggregate_pathway_genes[identifier]
            counts = [sum(bool(gene_counts[gene][stage]) for gene in members) for stage in stages]
            props.update(_profile(counts), backbone_gene_count=len(members),
                         direct_backbone_gene_count=len(pathway_genes[identifier]))
        elif label == "Metabolite":
            props.update(_profile([int(stage in metabolite_stages[identifier]) for stage in stages]))

    for from_stage, to_stage in pairwise(stages):
        before, after = stage_genes[from_stage], stage_genes[to_stage]
        for change, genes in (("added", after - before), ("removed", before - after)):
            for gene in genes:
                _edge(plan, "STAGE_DIFFERENTIAL", ("Gene", gene), ("Disease", stages[to_stage]),
                      f"{gene}|{from_stage}|{to_stage}|{change}", evidence=INFERRED,
                      properties={"from_stage": from_stage, "to_stage": to_stage, "change": change})


def build_v3_plan(data_dir=None, *, sources=DEFAULT_SOURCES, hmdb_xml=None):
    sources = Path(sources)
    manifest = read_source_manifest(sources)
    baseline = build_v2_plan(data_dir)
    v2_report = baseline.report()
    _initialize(baseline)
    pathway_ids = {identifier for label, identifier in baseline.nodes if label == "Pathway"}
    catalog = read_pathways(sources, pathway_ids)
    _add_pathways(baseline, catalog, manifest["files"])

    disease_aliases = defaultdict(set)
    for (label, identifier), props in baseline.nodes.items():
        if label == "Disease":
            for name in (*props["aliases"], *HMDB_DISEASE_ALIASES.get(props["stage_code"], ())):
                disease_aliases[name.casefold()].add(identifier)
    disease_ids = {alias: next(iter(identifiers)) for alias, identifiers in disease_aliases.items() if len(identifiers) == 1}
    hmdb = manifest["hmdb"]
    existing = {identifier for label, identifier in baseline.nodes if label == "Metabolite"}
    genes = {identifier: props for (label, identifier), props in baseline.nodes.items() if label == "Gene"}
    selection = select_hmdb(
        hmdb_xml or sources / hmdb["path"], hmdb, existing, genes, disease_ids,
        catalog, pathway_ids | catalog.names.keys(),
    )
    _add_hmdb(baseline, selection, hmdb, manifest["files"])
    for label, identifier in list(baseline.nodes):
        if label in NODE_KEYS:
            _is_a(baseline, (label, identifier), class_id(label))
    _add_profiles(baseline, catalog.parents)

    report = {
        "v2_plan": v2_report["plan"],
        "v2_input_reference_delta": v2_report["plan_delta_from_reference"],
        "archived_sources": manifest,
        "hmdb_selection": {
            "records_scanned": selection.scanned,
            "v2_metabolites_preserved": len(existing),
            "new_metabolites": len(selection.records) - len(existing),
            "new_by_route": dict(Counter(
                route for record in selection.records if record.identifier not in existing
                for route in record.properties["selection_routes"]
            )),
        },
        "pathway_coverage": {
            "v2_pathways_in_archived_hierarchy": sum(identifier in catalog.parents or identifier in catalog.names
                                                     for identifier in pathway_ids),
            "unresolved_v2_ids": sorted(pathway_ids - catalog.parents.keys() - catalog.names.keys()),
        },
    }
    return baseline, report
