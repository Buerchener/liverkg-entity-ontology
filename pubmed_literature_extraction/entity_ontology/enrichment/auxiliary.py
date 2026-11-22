"""Read bounded, typed context from primary databases alongside strict OLS evidence."""

import json
import re
from urllib.parse import quote, urlencode

from ..storage import atomic_json, digest, norm, read_json


def _reactome(node, definition, fetch):
    ident = definition.get("record_id", "")
    if not (definition.get("database") == "Reactome"
            and re.fullmatch(r"Reactome:R-[A-Z]{3}-\d+", ident) and ident in node["standard_ids"]):
        return []
    accession = ident.split(":")[1]
    url = f"https://release.reactome.org/ContentService/data/query/{accession}"
    record = fetch(url)
    if not record or record.get("stId") != accession or record.get("schemaClass") != "Pathway":
        return []
    facts = []
    go = record.get("goBiologicalProcess")
    if isinstance(go, dict) and go.get("databaseName") == "GO" and re.fullmatch(r"\d{7}", str(go.get("accession"))):
        facts.append({"subject_id": ident, "object_id": "GO:" + go["accession"],
                      "object_label": go.get("name"), "predicate": "goBiologicalProcess",
                      "relation": "RELATED", "source_url": url})
    # Reactome's event hierarchy gives ordered containment paths starting at the event.
    paths_url = f"https://release.reactome.org/ContentService/data/event/{accession}/ancestors"
    parents = {}
    for path in fetch(paths_url) or []:
        if (not isinstance(path, list) or len(path) < 2 or path[0].get("stId") != accession
                or path[1].get("schemaClass") != "Pathway"):
            continue
        parent = path[1]
        if re.fullmatch(r"R-[A-Z]{3}-\d+", str(parent.get("stId"))) and parent["stId"] != accession:
            parents[parent["stId"]] = parent.get("displayName")
    for parent, label in sorted(parents.items()):
        facts.append({"subject_id": ident, "object_id": "Reactome:" + parent, "object_label": label,
                      "predicate": "inverse(hasEvent)", "relation": "PART_OF", "source_url": paths_url})
    return facts


def _protein(node, fetch):
    ids = [*node["standard_ids"], *(i for row in node.get("prior_grounding", []) for i in row["standard_ids"])]
    accessions = {i.split(":")[1] for i in ids if re.fullmatch(r"UniProt:[A-Z0-9]{6,10}", i)}
    if len(accessions) != 1:
        return []
    accession, = accessions
    url = (f"https://rest.uniprot.org/uniprotkb/{accession}.json?"
           "fields=accession,reviewed,organism_id,gene_names,xref_interpro,cc_subunit,cc_similarity")
    record = fetch(url)
    if (not record or record.get("primaryAccession") != accession or "reviewed (Swiss-Prot)" not in record.get("entryType", "")
            or not node.get("taxon_id") or str(record.get("organism", {}).get("taxonId")) != str(node["taxon_id"])):
        return []
    facts = []
    for comment in record.get("comments", []):
        comment_type = comment.get("commentType")
        if comment_type in {"SUBUNIT", "SIMILARITY"}:
            for note in comment.get("texts", []):
                if note.get("value"):
                    facts.append({"predicate": comment_type, "text": note["value"], "references": note.get("evidences", [])})
    for entry in record.get("uniProtKBCrossReferences", []):
        if entry.get("database") == "InterPro" and re.fullmatch(r"IPR\d+", str(entry.get("id"))):
            facts.append({"predicate": "InterPro cross-reference", "object_id": entry["id"],
                          "object_label": next((p["value"] for p in entry.get("properties", [])
                                                if p["key"] == "EntryName"), None)})
    return [{**fact, "subject_id": "UniProt:" + accession, "relation": "RELATED", "source_url": url} for fact in facts]


def _mesh(node, fetch):
    # MeSH broader headings are indexing evidence. Even an exact label does not certify an OWL subclass.
    if any(t.get("parent_relations") for t in node.get("reference_terms", [])):
        return []
    label = node["name"]
    lookup_url = "https://id.nlm.nih.gov/mesh/lookup/descriptor?" + urlencode({"label": label, "match": "exact", "limit": 2})
    hits = fetch(lookup_url)
    if not isinstance(hits, list) or len(hits) != 1 or norm(hits[0].get("label", "")) != norm(label):
        return []
    matched = re.fullmatch(r"https?://id\.nlm\.nih\.gov/mesh/(D\d+)", hits[0].get("resource", ""))
    if not matched:
        return []
    ident = matched[1]
    url = f"https://id.nlm.nih.gov/mesh/{ident}.json"
    record = fetch(url)
    if (not record or record.get("identifier") != ident or norm(record.get("label", {}).get("@value", "")) != norm(label)
            or record.get("http://id.nlm.nih.gov/mesh/vocab#active") is not True):
        return []
    broader = record.get("broaderDescriptor", [])
    broader = [broader] if isinstance(broader, str) else broader
    facts = []
    for item in broader:
        parent = re.fullmatch(r"https?://id\.nlm\.nih\.gov/mesh/(D\d+)", item)
        if parent:
            facts.append({"subject_id": "MeSH:" + ident, "object_id": "MeSH:" + parent[1],
                          "predicate": "broaderDescriptor", "relation": "RELATED",
                          "source_url": url, "identity_lookup": lookup_url})
    return facts


def _chebi_metabolite_role(ident, fetch):
    accession = ident.split(":")[1]
    url = f"https://www.ebi.ac.uk/chebi/backend/api/public/compound/{accession}/"
    record = fetch(url)
    if not (isinstance(record, dict) and record.get("is_released") is True
            and record.get("chebi_accession") == ident and str(record.get("id")) == accession):
        return []
    outgoing = {
        "CHEBI:" + str(edge["final_id"]) for edge in record.get("ontology_relations", {}).get("outgoing_relations", [])
        if edge.get("relation_type") == "has role" and str(edge.get("init_id")) == accession
        and str(edge.get("final_id", "")).isdigit()
    }
    roles = [r for r in record.get("roles_classification", []) if r.get("biological_role") is True
             and r.get("chebi_accession") in outgoing]
    metabolite = "CHEBI:25212"
    # The label prioritizes one lookup within the two-read budget. The ancestry
    # of the role is checked independently before using it to type the compound.
    role = next((r for r in roles if r["chebi_accession"] == metabolite), None)
    if role is None:
        role = next((r for r in roles if str(r.get("name", "")).casefold().endswith(" metabolite")), None)
    if role is None:
        return []
    role_id = role["chebi_accession"]
    ancestry = {"role": role_id, "class": metabolite, "field": "exact_role", "url": url}
    if role_id != metabolite:
        obo = "http://purl.obolibrary.org/obo/"
        iri = obo + role_id.replace(":", "_")
        role_url = "https://www.ebi.ac.uk/ols4/api/v2/ontologies/chebi/classes/" + quote(quote(iri, safe=""), safe="")
        term = fetch(role_url)
        if not (isinstance(term, dict) and term.get("iri") == iri and term.get("isObsolete") is False
                and obo + "CHEBI_25212" in term.get("directAncestor", [])):
            return []
        ancestry.update(field="directAncestor", url=role_url)
    return [{"subject_id": ident, "object_id": role_id, "object_label": role["name"],
             "predicate": "has role", "relation": "HAS_ROLE", "source_url": url,
             "source_field": "ontology_relations.outgoing_relations", "role_ancestry": ancestry}]


def auxiliary_context(node, definition, directory, read_url):
    """At most two distinct reads, including cache hits. No returned predicate is a strict IS_A."""
    observations = []

    def fetch(url):
        if len(observations) >= 2:
            raise ValueError("Auxiliary evidence budget exceeded")
        path = directory / (digest(url) + ".json")
        observation = {"url": url, "cache_hit": path.is_file()}
        observations.append(observation)
        try:
            if observation["cache_hit"]:
                cached = read_json(path)
                if cached["url"] != url:
                    raise ValueError("Source URL mismatch")
                raw = cached["response"]
            else:
                raw = read_url(url)
            value = json.loads(raw)
            if not observation["cache_hit"]:
                atomic_json(path, {"url": url, "response": raw})
            observation["status"] = "observed"
            return value
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
            observation.update(status="lookup_failed", error=type(error).__name__)
            return None

    try:
        if node["kind"] == "Pathway":
            facts = _reactome(node, definition, fetch)
        elif node["kind"] == "Protein":
            facts = _protein(node, fetch)
        elif node["kind"] == "Metabolite":
            ids = {i for i in [*node.get("standard_ids", []), definition.get("record_id")]
                   if isinstance(i, str) and re.fullmatch(r"CHEBI:[0-9]+", i)}
            facts = _chebi_metabolite_role(next(iter(ids)), fetch) if len(ids) == 1 else _mesh(node, fetch)
        else:
            facts = []
    except (AttributeError, TypeError, KeyError, ValueError) as error:
        observations.append({"status": "invalid_response", "error": type(error).__name__})
        facts = []
    return facts, observations
