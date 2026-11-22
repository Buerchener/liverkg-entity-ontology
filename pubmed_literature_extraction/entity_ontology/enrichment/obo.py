"""Read sourced definitions and direct IS_A evidence from local OBO releases."""

from collections import defaultdict
import gzip
import hashlib
from pathlib import Path
import re
import unicodedata

from ..storage import norm
from ..hierarchy.ontology_ols import KIND_SOURCES

_ONTOLOGIES = {
    "CellType": ("CL", "Cell Ontology", "https://purl.obolibrary.org/obo/cl.obo"),
    "Tissue": ("UBERON", "Uberon", "https://purl.obolibrary.org/obo/uberon/basic.obo"),
    "Metabolite": ("CHEBI", "ChEBI", "https://purl.obolibrary.org/obo/chebi.obo"),
    "Disease": ("MONDO", "Mondo", "https://purl.obolibrary.org/obo/mondo.obo"),
}
_QUOTED = re.compile(r'^"((?:\\.|[^"\\])*)"(?:\s+(.*))?$')
_TYPE_NUMBER = re.compile(r"\btype (i{1,3}|[123])\b")
_ESCAPE = {"n": "\n", "t": "\t"}


def _quoted(text):
    match = _QUOTED.fullmatch(text)
    if not match:
        return "", ""
    value, qualifier = match.groups()
    value = re.sub(r"\\(.)", lambda character: _ESCAPE.get(character[1], character[1]), value)
    return value, qualifier or ""


def _cell_form(value):
    """Normalize accents, common plural cell labels, and 'type N' word order."""
    value = "".join(char for char in unicodedata.normalize("NFD", norm(value))
                    if unicodedata.category(char) != "Mn")
    value = re.sub(r"\bcells\b", "cell", value)
    # These are regular plurals of cell nouns. Keep modifiers such as stage,
    # compartment and species intact and require a unique active OBO match.
    value = re.sub(r"\b([a-z]*(?:cyte|blast|clast|troph|phil|neuron|phage|tid))s\b", r"\1", value)
    value = re.sub(r"\bprogenitors\b", "progenitor", value)
    value = re.sub(r"\bprogenitor cell\b", "progenitor", value)
    qualifier = _TYPE_NUMBER.search(value)
    if qualifier and (qualifier.start() == 0 or qualifier.end() == len(value)):
        number = qualifier[1]
        if number.startswith("i"):
            number = str(len(number))
        value = f"{value[:qualifier.start()]}{value[qualifier.end():]}".strip() + " type " + number
    return value


def _terms(path, prefix):
    version = ""
    terms = {}
    term = None
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for number, line in enumerate(stream, start=1):
            line = line.strip()
            if line.startswith("["):
                term = {"synonyms": [], "parents": {}} if line == "[Term]" else None
                continue
            key, separator, value = line.partition(": ")
            if not separator:
                continue
            if term is None:
                if key == "data-version":
                    version = value
                continue
            if key == "id":
                if value.startswith(prefix + ":"):
                    if value in terms:
                        raise ValueError(f"Duplicate OBO term {value} in {path}")
                    terms[value] = term
                else:
                    term = None
            elif key == "def":
                term["definition"], term["definition_xrefs"] = _quoted(value)
                term["definition_line"] = number
            elif key == "synonym":
                text, qualifier = _quoted(value)
                if text and qualifier.startswith("EXACT "):
                    term["synonyms"].append(text)
            elif key == "is_a":
                parent = value.split()[0]
                if parent.startswith(prefix + ":"):
                    term["parents"][parent] = number
            elif key in {"name", "is_obsolete"}:
                term[key] = value
    return {ident: term for ident, term in terms.items()
            if term.get("name") and term.get("is_obsolete") != "true"}, version


class OBOCatalog:
    """Index only the chosen OBO namespaces, with exact and conservative CL lookup."""

    def __init__(self, paths):
        self.metadata = {}
        self._indexes = {}
        for kind, source_path in paths.items():
            prefix, database, source_url = _ONTOLOGIES[kind]
            path = Path(source_path).resolve()
            terms, version = _terms(path, prefix)
            if not terms:
                raise ValueError(f"No active {prefix} terms found in {path}")
            with path.open("rb") as stream:
                sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
            self.metadata[kind] = {
                "path": str(path), "sha256": sha256, "release": version, "source_url": source_url,
            }
            index = defaultdict(dict)
            forms = _cell_form if kind == "CellType" else norm
            for ident, term in terms.items():
                for name in [term["name"], *term["synonyms"]]:
                    key = forms(name)
                    matches = index[key].setdefault(ident, [])
                    matches.append(name)
            self._indexes[kind] = (prefix, database, forms, terms, index)

    def inspect_term(self, source, term_id):
        """Read an active, named class by ID from a pinned local OBO file."""
        kind = next((kind for kind in self._indexes if KIND_SOURCES[kind] == source), None)
        if kind is None:
            return {"record": None}
        term = self._indexes[kind][3].get(term_id)
        if not term:
            return {"record": None}
        url = "https://purl.obolibrary.org/obo/" + term_id.replace(":", "_")
        release = self.metadata[kind]
        return {"record": {
            "term_id": term_id, "name": term["name"], "definition": term.get("definition"),
            "synonyms": sorted(set(term["synonyms"])), "source": source, "source_url": url,
            "artifact": {**release, "definition_line": term.get("definition_line")},
            "direct_parent_ids": sorted(term["parents"]),
            "parent_relations": [
                {"child_id": term_id, "parent_id": parent, "predicate": "is_a", "source": source,
                 "parent_source": source, "field": "is_a", "source_url": url,
                 "line": line, "artifact": release} for parent, line in sorted(term["parents"].items())
            ],
        }}

    def lookup(self, node):
        if node["kind"] not in self._indexes:
            return None
        prefix, database, form, terms, index = self._indexes[node["kind"]]
        query = node["name"]
        names = index.get(form(query), {})
        supplied = {ident for ident in node.get("standard_ids", []) if ident.startswith(prefix + ":")}
        if supplied and (len(supplied) != 1 or not supplied.issubset(terms) or names and not supplied.issubset(names)):
            return None
        if not supplied and len(names) != 1:
            return None
        ident, = supplied or names
        term = terms[ident]
        if not term.get("definition"):
            return None
        metadata = self.metadata[node["kind"]]
        matching_names = names.get(ident, [])
        exact = next((name for name in matching_names if norm(query) == norm(name)), None)
        return {
            "database": database,
            "url": "https://purl.obolibrary.org/obo/" + ident.replace(":", "_"),
            "definition": term["definition"],
            "field": "def",
            "record_id": ident,
            "canonical_label": term["name"],
            "aliases": sorted(set(term["synonyms"])),
            "direct_parent_ids": sorted(term["parents"]),
            "parent_records": [
                {"record_id": parent, "label": terms.get(parent, {}).get("name"),
                 "field": "is_a", "line": line}
                for parent, line in term["parents"].items()
            ],
            "match": "supplied_id" if supplied else ("exact_label_or_synonym" if exact else "normalized_cell_label_or_synonym"),
            "query": query,
            "matched_label": exact or next(iter(matching_names), None),
            "artifact": {**metadata, "definition_line": term["definition_line"]},
            "definition_xrefs": term["definition_xrefs"],
        }
