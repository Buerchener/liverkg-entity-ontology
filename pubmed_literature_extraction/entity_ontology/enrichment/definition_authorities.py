"""Bounded definition lookups for accession cross-references and cited clinical terms."""

import json
import re
from html import unescape
from urllib.parse import urlencode
from xml.etree import ElementTree

from ..storage import norm

CHEBI = "https://www.ebi.ac.uk/chebi/backend/api/public"
NCIT = "https://api-evsrest.nci.nih.gov/api/v1/concept/ncit"


def hmdb_definition(node, accession, read, observations):
    url = f"https://hmdb.ca/metabolites/{accession}.xml"
    root = ElementTree.fromstring(read(url))
    def field(name):
        return (root.findtext("{*}" + name) or "").strip()

    aliases = [s.text.strip() for s in root.findall("{*}synonyms/{*}synonym") if s.text]
    formula = (node.get("source") or {}).get("properties", {}).get("chemical_formula")
    if (root.tag.rsplit("}", 1)[-1] != "metabolite" or field("accession") != accession
            or norm(node["name"]) not in {norm(s) for s in [field("name"), *aliases]}
            or (formula and field("chemical_formula") and formula != field("chemical_formula"))
            or not field("description")):
        observations.append({"source": "HMDB", "url": url, "status": "identity_or_definition_unconfirmed"})
        return None
    observations.append({"source": "HMDB", "url": url, "status": "accepted"})
    return {"database": "HMDB", "url": url, "record_id": "HMDB:" + accession, "field": "description",
            "definition": field("description"), "canonical_label": field("name"), "aliases": aliases,
            "match": "supplied_id_and_label", "chemical_formula": field("chemical_formula")}


def hmdb_via_chebi(node, read, observations):
    """A search result is only a lead. Require the active compound's curated HMDB xref."""
    ids = {i.split(":", 1)[1] for i in node.get("standard_ids", []) if re.fullmatch(r"HMDB:HMDB\d{7}", i)}
    if node["kind"] != "Metabolite" or len(ids) != 1:
        return None
    accession, = ids
    search = CHEBI + "/es_search/?" + urlencode({"term": accession})
    hits = json.loads(read(search))
    results = hits.get("results", [])
    log = {"source": "ChEBI", "url": search, "query_id": accession, "hits": hits.get("total")}
    observations.append(log)
    if len(results) != 1 or hits.get("total") != 1:
        log["status"] = "missing_or_ambiguous_accession_hit"
        return None
    ident = results[0].get("_source", {}).get("chebi_accession", "")
    supplied = {i for i in node["standard_ids"] if i.startswith("CHEBI:")}
    if not re.fullmatch(r"CHEBI:\d+", ident) or (supplied and supplied != {ident}):
        log["status"] = "conflicting_accession"
        return None
    url = f"{CHEBI}/compound/{ident.split(':')[1]}/"
    data = json.loads(read(url))
    xrefs = [r for r in data.get("database_accessions", {}).get("MANUAL_X_REF", [])
             if r.get("source_name") == "HMDB" and r.get("accession_number") == accession]
    names = [r["name"] for group in data.get("names", {}).values() for r in group
             if r.get("status") == "C" and isinstance(r.get("name"), str)]
    checked_names = {norm(v) for v in [data.get("name", ""), *names]}
    props = (node.get("source") or {}).get("properties", {})
    formula = (data.get("chemical_data") or {}).get("formula")
    if (data.get("is_released") is not True or data.get("chebi_accession") != ident or not xrefs
            or norm(node["name"]) not in checked_names
            or (formula and props.get("chemical_formula") and props["chemical_formula"] != formula)
            or not data.get("definition")):
        log.update(status="xref_name_formula_or_definition_unconfirmed", detail_url=url)
        return None
    edges = []
    for rel in data.get("ontology_relations", {}).get("outgoing_relations", []):
        if (rel.get("relation_type") == "is a" and rel.get("init_id") == data.get("id")
                and type(rel.get("final_id")) is int):
            edges.append({"child_id": ident, "parent_id": f"CHEBI:{rel['final_id']}", "predicate": "is_a",
                          "source": "ChEBI", "parent_source": "ChEBI", "source_url": url,
                          "field": "ontology_relations.outgoing_relations", "source_predicate": "is a"})
    log.update(status="accepted", detail_url=url, record_id=ident)
    return {"database": "ChEBI", "url": url, "record_id": ident, "field": "definition",
            "definition": unescape(re.sub(r"<[^>]+>", "", data["definition"])).strip(),
            "canonical_label": data["name"], "aliases": sorted(set(names)), "match": "curated_hmdb_xref_and_name",
            "cross_reference": {"kg_id": "HMDB:" + accession, "field": "database_accessions.MANUAL_X_REF",
                                "entry": xrefs[0], "chemical_formula": formula, "search_url": search},
            "parent_relations": edges, "direct_parent_ids": sorted({e["parent_id"] for e in edges}),
            "verified_standard_ids": [ident]}


def ncit_definition(node, read, observations):
    """Only a unique active preferred name with the original mention (or supplied ID)."""
    ids = {i.split(":", 1)[1] for i in node["standard_ids"] if i.startswith("NCIT:")}
    if len(ids) > 1:
        observations.append({"source": "NCIt", "status": "ambiguous_source_ids", "term_ids": sorted(ids)})
        return None
    name = node["name"]
    if ids:
        code, = ids
        if not re.fullmatch(r"C\d+", code):
            return None
        search = None
    else:
        search = NCIT + "/search?" + urlencode({"term": name, "type": "match", "include": "summary", "pageSize": 10})
        hits = json.loads(read(search))
        candidates = hits.get("concepts", [])
        codes = {r["code"] for r in candidates if r.get("active") is True and r.get("terminology") == "ncit"
                 and norm(r.get("name", "")) == norm(name) and re.fullmatch(r"C\d+", r.get("code", ""))}
        observations.append({"source": "NCIt", "url": search, "returned": len(candidates),
                             "total": hits.get("total"), "exact_preferred_codes": sorted(codes)})
        if hits.get("total") != len(candidates) or len(codes) != 1:
            return None
        code, = codes
    url = f"{NCIT}/{code}?include=summary"
    data = json.loads(read(url))
    definition = next((d["definition"] for d in data.get("definitions", []) if d.get("source") == "NCI"
                       and isinstance(d.get("definition"), str) and d["definition"].strip()), None)
    if (not definition or data.get("code") != code or data.get("active") is not True
            or data.get("terminology") != "ncit" or norm(data.get("name", "")) != norm(name)):
        return None
    types = {prop["value"] for prop in data.get("properties", []) if prop.get("code") == "P106" and prop.get("value")}
    expected = {"Tissue": {"Tissue"}, "Disease": {"Disease or Syndrome", "Neoplastic Process",
                                                  "Mental or Behavioral Dysfunction", "Congenital Abnormality"}}
    result = {"database": "NCIt", "url": url, "record_id": "NCIT:" + code, "canonical_label": data["name"],
              "field": "definitions[NCI]", "definition": definition, "authority_version": data.get("version"),
              "semantic_types": sorted(types), "search_url": search, "match": "supplied_id_and_label" if ids
              else "exact_cited_preferred_name"}
    if not types or not types.issubset(expected[node["kind"]]):
        result["scope_review_required"] = "NCIt semantic type does not establish membership of the local entity type."
    else:
        result["verified_standard_ids"] = ["NCIT:" + code]
    observations.append({"source": "NCIt", "url": url, "record_id": result["record_id"],
                         "status": "external_definition_scope_review" if result.get("scope_review_required") else "accepted"})
    return result
