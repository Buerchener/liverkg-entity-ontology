"""Stream the frozen HMDB XML and select metabolites with traceable liver context."""

from collections import defaultdict
from dataclasses import dataclass, field
import gzip
import hashlib
import re
import xml.etree.ElementTree as ET


TAXONOMY_LEVELS = ("kingdom", "super_class", "class", "sub_class")
HEPATIC_TISSUES = {"liver", "hepatic tissue", "hepatocytes"}
PROTEIN_ROLES = {"enzyme", "transporter"}


class _HashedReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()
        self.bytes = 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        self.bytes += len(data)
        return data


def _records(stream):
    events = ET.iterparse(stream, events=("start", "end"))
    _, root = next(events)
    ns = root.tag.split("}", 1)[0] + "}" if root.tag.startswith("{") else ""
    if root.tag != ns + "hmdb":
        raise ValueError("The HMDB archive does not contain an HMDB XML root.")
    namespaces = {"": ns[1:-1]} if ns else {}
    depth = 1
    for event, node in events:
        if event == "start":
            depth += 1
            continue
        if depth == 2 and node.tag == ns + "metabolite":
            yield node, namespaces
            root.remove(node)
        depth -= 1


def _text(node, path, namespaces):
    return (node.findtext(path, default="", namespaces=namespaces) or "").strip()


def _values(node, path, namespaces):
    return {item.text.strip() for item in node.findall(path, namespaces) if (item.text or "").strip()}


def _chemical_family(classification, direct_parent):
    groups = [classification.get("sub_class", "").casefold(), direct_parent.casefold()]
    if any("bile acid" in name or "bile salt" in name for name in groups):
        return "bile_acid"
    if any("ceramide" in name for name in groups):
        return "ceramide"
    if classification.get("class", "").casefold() == "fatty acyls":
        if any("fatty acid" in name for name in groups):
            return "fatty_acid"
    return ""


def _gene_matches(node, namespaces, by_symbol, by_uniprot):
    matches = defaultdict(lambda: defaultdict(set))
    for protein in node.findall("protein_associations/protein", namespaces):
        role = _text(protein, "protein_type", namespaces)
        if role.casefold() not in PROTEIN_ROLES:
            continue
        symbol = _text(protein, "gene_name", namespaces).upper()
        uniprot = _text(protein, "uniprot_id", namespaces).upper()
        symbol_matches = by_symbol.get(symbol, set())
        protein_matches = by_uniprot.get(uniprot, set())
        candidates = symbol_matches & protein_matches if symbol_matches and protein_matches else symbol_matches | protein_matches
        if len(candidates) != 1:
            continue
        gene, = candidates
        matched = matches[gene]
        matched["hmdb_protein_roles"].add(role)
        if uniprot:
            matched["hmdb_uniprot_ids"].add(uniprot)
        accession = _text(protein, "protein_accession", namespaces)
        if accession:
            matched["hmdb_protein_accession_list"].add(accession)
    return {identifier: {key: sorted(values) for key, values in group.items()} for identifier, group in matches.items()}


def _disease_matches(node, namespaces, disease_ids):
    matches = defaultdict(lambda: defaultdict(set))
    for disease in node.findall("diseases/disease", namespaces):
        name = _text(disease, "name", namespaces)
        identifier = disease_ids.get(name.casefold())
        if identifier is None:
            continue
        matched = matches[identifier]
        matched["hmdb_disease_names"].add(name)
        matched["hmdb_pmids"].update(
            pmid for pmid in _values(disease, "references/reference/pubmed_id", namespaces) if pmid.isdecimal())
        omim = _text(disease, "omim_id", namespaces)
        if omim:
            matched["hmdb_disease_omim_ids"].add(omim)
    return {identifier: {key: sorted(values) for key, values in group.items()} for identifier, group in matches.items()}


def _xrefs(node, namespaces, accession):
    result = {
        f"HMDB:{value}" for value in _values(node, "secondary_accessions/accession", namespaces)
        if re.fullmatch(r"HMDB\d+", value)
    }
    result.add(f"HMDB:{accession}")
    for tag, namespace, pattern in (
        ("chebi_id", "CHEBI", r"\d+"),
        ("kegg_id", "KEGG.COMPOUND", r"C\d{5}"),
        ("pubchem_compound_id", "PUBCHEM.COMPOUND", r"\d+"),
    ):
        value = _text(node, tag, namespaces)
        if re.fullmatch(pattern, value):
            result.add(f"{namespace}:{value}")
    return result


def _pathway_matches(node, namespaces, xrefs, catalog, pathway_ids):
    matches = defaultdict(set)
    for xref in xrefs:
        for identifier in catalog.compounds.get(xref, ()):
            matches[identifier].add(("KEGG", xref))
        for identifier in catalog.chebi.get(xref, ()):
            matches[identifier].add(("Reactome", xref))
    for identifier in _values(node, "biological_properties/pathways/pathway/kegg_map_id", namespaces):
        match = re.fullmatch(r"(?:map|hsa)(\d{5})", identifier)
        human = f"KEGG:hsa{match[1]}" if match else None
        if human in pathway_ids:
            matches[human].add(("HMDB", identifier))
    return dict(matches)


@dataclass
class Metabolite:
    identifier: str
    properties: dict
    genes: dict
    diseases: dict
    pathways: dict


@dataclass
class HMDBSelection:
    records: list[Metabolite] = field(default_factory=list)
    scanned: int = 0


def select_hmdb(archive, source, existing, genes, disease_ids, catalog, pathway_ids) -> HMDBSelection:
    """Verify the decompressed hash and stream only admitted records into memory.

    The strict route needs both a backbone enzyme/transporter and a listed
    stage disease. The lipid route additionally admits curated families with a
    hepatobiliary pathway, or a selected lipid pathway and liver-specific tissue
    or disease evidence. The frozen v2 set is retained independently.
    """
    by_symbol, by_uniprot = defaultdict(set), defaultdict(set)
    for identifier, gene in genes.items():
        by_symbol[gene["gene_symbol"].upper()].add(identifier)
        for uniprot in gene.get("protein_ids_from_disgenet", "").split(";"):
            if uniprot.strip():
                by_uniprot[uniprot.strip().upper()].add(identifier)
    selection = HMDBSelection()
    selected_ids = set()
    with gzip.open(archive, "rb") as compressed:
        reader = _HashedReader(compressed)
        for node, namespaces in _records(reader):
            selection.scanned += 1
            accession = _text(node, "accession", namespaces)
            identifier = f"HMDB:{accession}"
            baseline = identifier in existing
            classification = {
                level: value for level in TAXONOMY_LEVELS
                if (value := _text(node, f"taxonomy/{level}", namespaces))
            }
            direct_parent = _text(node, "taxonomy/direct_parent", namespaces)
            family = _chemical_family(classification, direct_parent)
            diseases = _disease_matches(node, namespaces, disease_ids)
            if not (baseline or family or diseases):
                continue
            xrefs = _xrefs(node, namespaces, accession)
            pathways = _pathway_matches(node, namespaces, xrefs, catalog, pathway_ids)
            tissues = _values(node, "biological_properties/tissue_locations/tissue", namespaces)
            liver_tissue = any(tissue.casefold() in HEPATIC_TISSUES for tissue in tissues)
            hepatic_pathway = bool(pathways.keys() & catalog.hepatobiliary)
            lipid_liver_pathway = bool(pathways.keys() & catalog.liver_with_tissue) and (liver_tissue or bool(diseases))
            genes_found = _gene_matches(node, namespaces, by_symbol, by_uniprot)
            routes = []
            if baseline:
                routes.append("v2")
            if genes_found and diseases:
                routes.append("enzyme_or_transporter_and_stage_disease")
            if family and (hepatic_pathway or lipid_liver_pathway):
                routes.append("liver_pathway_and_lipid_class")
            if not routes:
                continue

            if not re.fullmatch(r"HMDB\d+", accession) or identifier in selected_ids:
                raise ValueError(f"Invalid or repeated HMDB accession {accession!r}.")
            selected_ids.add(identifier)
            version = _text(node, "version", namespaces)
            if f"HMDB {version}" != source["version"]:
                raise ValueError(f"Unexpected HMDB XML version {version!r} for {accession}.")
            aliases = sorted(_values(node, "synonyms/synonym", namespaces))
            name = _text(node, "name", namespaces)
            if not name:
                raise ValueError(f"Empty name for {accession}.")
            props = {
                "metabolite_id": identifier, "name": name, "hmdb_id": accession,
                "aliases": aliases, "xrefs": sorted(xrefs), "selection_routes": routes,
                "liver_pathway_ids": sorted(pathways.keys() & (catalog.hepatobiliary | catalog.liver_with_tissue)),
                **classification,
            }
            if formula := _text(node, "chemical_formula", namespaces):
                props["chemical_formula"] = formula
            if family:
                props["chemical_family"] = family
            if direct_parent:
                props["direct_parent"] = direct_parent
            if liver_tissue:
                props["hmdb_liver_tissues"] = sorted(t for t in tissues if t.casefold() in HEPATIC_TISSUES)
            selection.records.append(Metabolite(identifier, props, genes_found, diseases, pathways))
    if reader.bytes != source["uncompressed_bytes"] or reader.digest.hexdigest() != source["sha256"]:
        raise ValueError("The uncompressed HMDB XML differs from the handoff checksum.")
    if missing := existing - selected_ids:
        raise ValueError(f"The HMDB archive is missing v2 accessions: {', '.join(sorted(missing))}.")
    return selection
