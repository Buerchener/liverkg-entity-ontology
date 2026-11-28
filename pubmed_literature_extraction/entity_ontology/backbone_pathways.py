"""Read frozen Reactome and KEGG files for the v3 pathway subgraph."""

from collections import defaultdict
import csv
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re


REACTOME_FILES = (
    "ReactomePathways.txt",
    "ReactomePathwaysRelation.txt",
    "ChEBI2Reactome.txt",
    "Pathways2GoTerms_human.txt",
)
KEGG_FILES = ("kegg_br08901.json", "kegg_compound_pathways.tsv")
REACTOME_HEPATOBILIARY = {"R-HSA-192105", "R-HSA-194068"}
REACTOME_LIPID = {"R-HSA-77289", "R-HSA-428157", "R-HSA-5626978"}
KEGG_HEPATOBILIARY = {"hsa00120", "hsa04976", "hsa04932"}
KEGG_LIPID = {"hsa00061", "hsa00062", "hsa00071", "hsa01212", "hsa00600", "hsa04071", "hsa04979"}


def table_rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        yield from csv.reader(stream, delimiter="\t")


def file_sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_source_manifest(folder: Path) -> dict:
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    for name in (*REACTOME_FILES, *KEGG_FILES):
        row = manifest["files"][name]
        path = folder / name
        if path.stat().st_size != row["bytes"] or file_sha256(path) != row["sha256"]:
            raise ValueError(f"The archived pathway source {name} differs from its manifest.")
    return manifest


def source_version(record):
    return f"{record['version']}:sha256:{record['sha256']}"


def ancestors(starts, parents):
    seen = set()
    pending = list(starts)
    while pending:
        identifier = pending.pop()
        if identifier in seen:
            continue
        seen.add(identifier)
        pending.extend(parents.get(identifier, ()))
    return seen


@dataclass
class PathwayCatalog:
    names: dict[str, str]
    parents: dict[str, set[str]]
    kegg_groups: set[str]
    compounds: dict[str, set[str]]
    chebi: dict[str, set[str]]
    go: dict[str, set[str]]
    hepatobiliary: set[str]
    liver_with_tissue: set[str]


def _reactome(folder, seeds):
    names = {f"Reactome:{identifier}": name for identifier, name, species in table_rows(folder / REACTOME_FILES[0])
             if species == "Homo sapiens" and identifier.startswith("R-HSA-")}
    full_parents = defaultdict(set)
    for parent, child in table_rows(folder / REACTOME_FILES[1]):
        left, right = f"Reactome:{child}", f"Reactome:{parent}"
        if left in names and right in names:
            full_parents[left].add(right)

    selected = ancestors(seeds.intersection(names), full_parents)
    parents = {child: full_parents[child] for child in selected if child in full_parents}
    names = {identifier: names[identifier] for identifier in selected}
    go = defaultdict(set)
    for identifier, _, go_id in table_rows(folder / REACTOME_FILES[3]):
        pathway = f"Reactome:{identifier}"
        if pathway in selected and re.fullmatch(r"GO:\d{7}", go_id):
            go[pathway].add(go_id)
    chebi = defaultdict(set)
    for identifier, pathway_id, _, _, evidence, species in table_rows(folder / REACTOME_FILES[2]):
        pathway = f"Reactome:{pathway_id}"
        if pathway in selected and species == "Homo sapiens" and identifier.isdecimal():
            chebi[f"CHEBI:{identifier}"].add(pathway)

    def under(roots):
        prefixed = {f"Reactome:{root}" for root in roots}
        return {identifier for identifier in selected if ancestors([identifier], parents) & prefixed}

    return names, parents, chebi, go, under(REACTOME_HEPATOBILIARY), under(REACTOME_LIPID)


def _kegg(folder, seeds):
    brite = json.loads((folder / KEGG_FILES[0]).read_text(encoding="utf-8"))
    if brite.get("name") != "br08901":
        raise ValueError("Expected KEGG BRITE br08901.")
    parents = defaultdict(set)
    groups = {}

    def visit(record, chain):
        children = record.get("children")
        name = record["name"].strip()
        if children:
            group = "KEGG-BRITE:br08901:" + hashlib.sha256(
                "\0".join((*chain, name)).encode()).hexdigest()[:20]
            groups[group] = name
            hits = {visit(child, (*chain, name)) for child in children} - {None}
            for hit in hits:
                parents[hit].add(group)
            return group if hits else None
        match = re.match(r"(\d{5})\s+", name)
        identifier = f"KEGG:hsa{match[1]}" if match else ""
        return identifier if identifier in seeds else None

    for category in brite["children"]:
        visit(category, ())
    used = {parent for values in parents.values() for parent in values}
    compounds = defaultdict(set)
    for map_id, compound in table_rows(folder / KEGG_FILES[1]):
        if not map_id.startswith("path:map") or not re.fullmatch(r"cpd:C\d{5}", compound):
            continue
        pathway = f"KEGG:hsa{map_id.removeprefix('path:map')}"
        if pathway in seeds:
            compounds[f"KEGG.COMPOUND:{compound.removeprefix('cpd:')}"].add(pathway)
    return {key: groups[key] for key in used}, dict(parents), compounds


def read_pathways(folder: Path, seeds: set[str]) -> PathwayCatalog:
    rn, rp, chebi, go, hepatic, lipid = _reactome(folder, seeds)
    kn, kp, compounds = _kegg(folder, seeds)
    if not rp or not kp:
        raise ValueError("The archived pathway sources did not resolve both v2 pathway hierarchies.")
    return PathwayCatalog(
        names={**rn, **kn}, parents={**rp, **kp}, kegg_groups=set(kn), compounds=compounds, chebi=chebi, go=go,
        hepatobiliary=hepatic | {f"KEGG:{identifier}" for identifier in KEGG_HEPATOBILIARY},
        liver_with_tissue=lipid | {f"KEGG:{identifier}" for identifier in KEGG_LIPID},
    )
