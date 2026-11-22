"""Recover sourced definitions without asking a model to write one."""

from collections.abc import Iterable
from copy import deepcopy
from html import unescape
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree.ElementTree import ParseError

from ..hierarchy.ols_client import OLSError
from ..hierarchy.ontology_ols import OntologyOLS, term_source
from ..storage import atomic_json, digest, norm, read_json
from .definition_authorities import hmdb_definition, hmdb_via_chebi, ncit_definition
from .obo import _cell_form

_OLS_BY_KIND = {
    "Disease": ("MONDO", "MONDO"),
    "CellType": ("CL", "CL"),
    "Tissue": ("Uberon", "UBERON"),
    "Metabolite": ("ChEBI", "CHEBI"),
}
_GENE = re.compile(r"NCBIGene:(\d+)\Z")
_REACTOME = re.compile(r"Reactome:(R-[A-Z]{3}-\d+)\Z")
_KEGG = re.compile(r"KEGG_PATHWAY:([a-z]{2,4}\d{5})\Z")
_PMID = re.compile(r"PubMed:(\d+)\Z", re.I)
_ABBREVIATION = re.compile(r"[A-Z]{2,8}\Z")


def _abbreviation(text, label):
    if not _ABBREVIATION.fullmatch(label):
        return None
    expansions = {}
    for mention in re.finditer(r"\(" + label + r"\)", text):
        before = text[:mention.start()].rstrip()
        suffix = re.search(r"[A-Za-z]+(?:[ -][A-Za-z]+){" + str(len(label) - 1) + r"}$", before)
        if suffix:
            words = re.findall(r"[A-Za-z]+", suffix[0])
            if "".join(word[0].upper() for word in words) == label:
                expansions[norm(suffix[0])] = suffix[0]
    return next(iter(expansions.values())) if len(expansions) == 1 else None


def _read_url(url):
    request = Request(url, headers={"User-Agent": "LiverKG-Ontology-Enrichment/1.0", "Accept": "application/json, */*;q=0.5"})
    with urlopen(request, timeout=12) as response:
        return response.read().decode("utf-8")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _source(name, url, definition, **evidence):
    return {"database": name, "url": url, "definition": definition, **evidence}


def _article_definition(node, context):
    """Use only explicit definitional wording from the KG node's own cited paper."""
    if not context.get("pmid"):
        return None
    labels = [node["name"]]
    if context.get("attested_expansion"):
        labels.append(context["attested_expansion"])
    patterns = [re.compile(r"^(?:the term\s+)?[\"'‘“]?" + re.escape(label) +
                           r"[\"'’”]?\s+(?:is defined as|are defined as|refers to|denotes)\s+\S",
                           flags=re.I) for label in labels]
    definitions = {s.strip() for s in context.get("mention_excerpts", [])
                   if any(pattern.search(s.strip()) for pattern in patterns)}
    if len(definitions) != 1:
        return None
    return _source("PubMed", context["url"], definitions.pop(), field="explicit definition in original article",
                   source_record_id="PubMed:" + context["pmid"], match="cited_subject_definition",
                   artifact=context["artifact"], canonical_label=node["name"])


def _gene(url, body, accession, node):
    record = json.loads(body)["result"].get(accession, {})
    organism = record.get("organism") or {}
    if str(record.get("uid")) != accession or record.get("status"):
        return None
    symbol = _text(record.get("name"))
    if norm(symbol) != norm(node["name"]) or str(organism.get("taxid")) != "9606":
        return None
    official_name = _text(record.get("nomenclaturename") or record.get("description"))
    if not official_name:
        return None
    # A Gene summary often describes its product. Keep the authoritative gene
    # identity explicit and retain the summary as a separately attributed field.
    summary = _text(record.get("summary"))
    definition = f"Human gene {symbol}. NCBI Gene name: {official_name}."
    if summary:
        definition += f" NCBI Gene summary: {summary}"
    return _source("NCBI Gene", url, definition, field="name + nomenclaturename / description + optional summary",
                   record_id=f"NCBIGene:{accession}", canonical_label=symbol, taxon_id="9606",
                   field_values={"name": symbol, "official_name": official_name, "summary": summary})


def _hgnc(url, body, accession, node):
    records = json.loads(body)["response"]["docs"]
    if len(records) != 1:
        return None
    record = records[0]
    if (str(node.get("taxon_id") or "9606").removeprefix("NCBITaxon:") != "9606"
            or str(record.get("entrez_id")) != accession or record.get("status") != "Approved"
            or norm(_text(record.get("symbol"))) != norm(node["name"])):
        return None
    name, locus = _text(record.get("name")), _text(record.get("locus_type"))
    if not name or not locus:
        return None
    return _source("HGNC", url, f"{name}. HGNC locus type: {locus}.",
                   field="name + locus_type", field_values={"name": name, "locus_type": locus},
                   record_id=record["hgnc_id"], canonical_label=record["symbol"], taxon_id="9606")


def _reactome(url, body, accession, node):
    record = json.loads(body)
    if (record.get("stId") != accession or record.get("schemaClass") != "Pathway"
            or norm(_text(record.get("displayName"))) != norm(node["name"])):
        return None
    summaries = [_text(item.get("text")) for item in record.get("summation", [])]
    definition = next((unescape(re.sub(r"<[^>]+>", " ", line)).strip() for line in summaries if line), "")
    if not definition:
        return None
    return _source("Reactome", url, definition, field="summation.text", record_id="Reactome:" + accession,
                   canonical_label=record["displayName"], schema_class=record["schemaClass"])


def _kegg(url, body, accession, node):
    fields = {}
    key = ""
    for line in body.splitlines():
        key = line[:12].strip() or key
        fields.setdefault(key, []).append(line[12:].strip())
    if accession not in " ".join(fields.get("ENTRY", [])).split():
        return None
    label = " ".join(fields.get("NAME", []))
    if norm(label.split(" - ")[0]) != norm(node["name"]):
        return None
    definition = " ".join(fields.get("DESCRIPTION", []))
    return _source("KEGG", url, definition, field="DESCRIPTION", record_id="KEGG_PATHWAY:" + accession,
                   canonical_label=label) if definition else None


def _source_requests(node):
    for standard_id in node.get("standard_ids", []):
        if node["kind"] == "Gene" and (match := _GENE.fullmatch(standard_id)):
            accession = match[1]
            yield (_gene, "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" +
                   urlencode({"db": "gene", "id": accession, "retmode": "json"}), accession)
            yield _hgnc, f"https://rest.genenames.org/fetch/entrez_id/{accession}", accession
        if node["kind"] == "Pathway" and (match := _REACTOME.fullmatch(standard_id)):
            yield _reactome, f"https://release.reactome.org/ContentService/data/query/{match[1]}", match[1]
        if node["kind"] == "Pathway" and (match := _KEGG.fullmatch(standard_id)):
            yield _kegg, f"https://rest.kegg.jp/get/{match[1]}", match[1]


class DefinitionSources:
    def __init__(self, directory, *, articles: Iterable = (), read_url=None, catalog=None, allow_ols=True, obo=None):
        self.directory = Path(directory)
        self.catalog = catalog if catalog is not None else OntologyOLS()
        self.allow_ols = allow_ols
        self.obo = obo
        self.read_url = read_url or _read_url
        self.articles = {}
        for path in articles:
            with Path(path).open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        row = json.loads(line)
                        self.articles.setdefault(str(row.get("pmid")), (row, str(Path(path).resolve())))

    def _context(self, node):
        label = node.get("source_context", {}).get("source", "")
        match = _PMID.fullmatch(label) if isinstance(label, str) else None
        if not match or match[1] not in self.articles:
            return {}
        row, path = self.articles[match[1]]
        query = norm(node["name"])
        text = _text(row.get("abstract") or row.get("text"))
        passages = [_text(row.get("title")), *re.split(r"(?<=[.!?])\s+", text)]
        mentions = [part.strip() for part in passages
                    if re.search(r"(?<!\w)" + re.escape(query) + r"(?!\w)", norm(part))]
        context = {"url": f"https://pubmed.ncbi.nlm.nih.gov/{match[1]}/", "pmid": match[1],
                   "title": row.get("title"), "mention_excerpts": mentions[:3], "artifact": path}
        if expansion := _abbreviation("\n".join(passages), node["name"]):
            context["attested_expansion"] = expansion
        return context

    def _body(self, url):
        path = self.directory / "raw" / f"{digest(url)}.json"
        if path.is_file():
            return read_json(path)["response"]
        body = self.read_url(url)
        atomic_json(path, {"url": url, "response": body})
        return body

    def _lookup(self, node, context):
        observations = []
        original = _text(node.get("definition"))
        if original:
            source = node["source"]
            return _source(node.get("source_context", {}).get("source") or "LiverKG", None, original,
                           field="LiverKG description or annotation",
                           neo4j_database=source.get("database"), element_id=source.get("element_id")), observations
        for parse, url, accession in _source_requests(node):
            try:
                evidence = parse(url, self._body(url), accession, node)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                observations.append({"status": "request_or_response_error", "url": url,
                                     "error": type(error).__name__,
                                     "http_status": error.code if isinstance(error, HTTPError) else None})
            else:
                observations.append({"url": url, "status": "accepted" if evidence else "no_usable_definition"})
                if evidence:
                    return evidence, observations
        expansion = context.get("attested_expansion")
        query = {**node, "name": expansion} if expansion else node
        source_label = node.get("source_context", {}).get("source")
        has_pubmed_reference = isinstance(source_label, str) and _PMID.fullmatch(source_label)
        source_mention = bool(context.get("mention_excerpts"))
        standard_ids = node.get("standard_ids", [])
        generic_label = norm(node["name"]) in {norm(node["kind"]), norm(node["kind"]) + "s"}
        if generic_label and not standard_ids:
            return None, [*observations, {"status": "generic_type_without_specific_identity",
                                          "source_mention": source_mention}]
        hmdb_ids = {i for i in standard_ids if re.fullmatch(r"HMDB:HMDB\d{7}", i)}
        if self.allow_ols and node["kind"] == "Metabolite" and hmdb_ids:
            if len(hmdb_ids) != 1:
                return None, [*observations, {"status": "ambiguous_source_ids", "term_ids": sorted(hmdb_ids)}]
            try:
                chemical = hmdb_via_chebi(node, self._body, observations)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                observations.append({"source": "ChEBI", "status": "lookup_failed", "error": str(error)})
            else:
                if chemical:
                    return chemical, observations
            try:
                accession = next(iter(hmdb_ids)).split(":")[1]
                chemical = hmdb_definition(node, accession, self._body, observations)
            except (OSError, ParseError) as error:
                observations.append({"source": "HMDB", "status": "lookup_failed", "error": str(error)})
                chemical = None
            return chemical, observations
        if self.allow_ols and node["kind"] in {"Disease", "Tissue"} and any(i.startswith("NCIT:") for i in standard_ids):
            try:
                return ncit_definition(query, self._body, observations), observations
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                return None, [*observations, {"source": "NCIt", "status": "lookup_failed", "error": str(error)}]

        prefix = _OLS_BY_KIND.get(node["kind"], (None, None))[1]
        prior = [row for row in node.get("prior_grounding", [])
                 if row.get("artifact_record", {}).get("path") and row["artifact_record"].get("json_pointer")]
        hints = {i for row in prior for i in row.get("standard_ids", []) if prefix and i.startswith(prefix + ":")}
        supplied = {i for i in standard_ids if prefix and i.startswith(prefix + ":")}
        if len(hints | supplied) > 1:
            return None, [*observations, {"status": "ambiguous_source_ids", "term_ids": sorted(hints | supplied)}]
        if hints and not supplied:
            query = {**query, "standard_ids": [*standard_ids, *hints]}

        def compatible_hint(ident, label, aliases):
            if not hints:
                return True
            form = _cell_form if node["kind"] == "CellType" else norm
            return ident in hints and form(query["name"]) in {form(value) for value in [label, *aliases]}

        def bind_hint(evidence):
            if hints:
                return {**evidence, "prior_grounding": prior, "verified_standard_ids": sorted(hints)}
            return evidence

        if self.obo and (not has_pubmed_reference or source_mention):
            local = self.obo.lookup(query)
            if local and compatible_hint(local["record_id"], local["canonical_label"], local["aliases"]):
                return bind_hint(local), observations
        if self.allow_ols and node["kind"] in _OLS_BY_KIND:
            source, prefix = _OLS_BY_KIND[node["kind"]]
            official_ids = [value for value in query.get("standard_ids", []) if value.startswith(prefix + ":")]
            if len(official_ids) > 1:
                return None, [*observations, {"status": "ambiguous_source_ids", "source": source,
                                              "term_ids": official_ids}]
            try:
                if official_ids:
                    inspected = self.catalog.inspect_term(source, official_ids[0])
                    terms = [inspected["record"]] if inspected["record"] else []
                    result = {"terms": terms, "reason": None}
                else:
                    result = self.catalog.verify_endpoint(source, query)
                observations.append({"source": source, "result": result})
            except (OLSError, OSError, ValueError, KeyError, TypeError) as error:
                observations.append({"source": source, "status": "lookup_failed", "error": type(error).__name__})
                result = {"terms": []}
            term_list = result["terms"]
            if len(term_list) == 1 and term_list[0].get("definition"):
                term = term_list[0]
                # Text-mined labels without a stable identifier need a traceable mention.
                if not official_ids and has_pubmed_reference and not source_mention:
                    return None, observations
                if not compatible_hint(term["term_id"], term["name"], term.get("synonyms", [])):
                    return None, [*observations, {"status": "prior_identifier_label_scope_unconfirmed",
                                                  "term_id": term["term_id"]}]
                return bind_hint(_source(source, term["source_url"], term["definition"], field="definition",
                               record_id=term["term_id"], canonical_label=term["name"],
                               aliases=term.get("synonyms", []), parent_relations=term.get("parent_relations", []),
                               direct_parent_ids=term["direct_parent_ids"], match=term.get("match", "supplied_id"))), observations
        if self.allow_ols and node["kind"] in {"Disease", "Tissue"} and not hints and not standard_ids and source_mention:
            try:
                external = ncit_definition(query, self._body, observations)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                observations.append({"source": "NCIt", "status": "lookup_failed", "error": str(error)})
            else:
                if external:
                    return external, observations
        if paper := _article_definition(node, context):
            observations.append({"source": "PubMed", "status": "explicit_definition", "url": paper["url"]})
            return paper, observations
        return None, observations

    def _reference_terms(self, node, evidence, log):
        """Retain confirmed class identity, provenance and direction at the point of grounding."""
        ident = evidence.get("record_id")
        source = term_source(ident)
        identity = {k: evidence[k] for k in ("record_id", "url", "canonical_label", "match", "artifact")
                    if k in evidence}
        if self.obo and source:
            local = self.obo.inspect_term(source, ident)["record"]
            if local:
                return [{**local, "match": evidence.get("match", "supplied_id"), "identity_evidence": identity}]
        if source and evidence.get("field") == "definition":
            return [{"source": source, "term_id": ident, "name": evidence["canonical_label"],
                     "definition": evidence["definition"], "source_url": evidence["url"],
                     "synonyms": evidence.get("aliases", []), "direct_parent_ids": evidence["direct_parent_ids"],
                     "parent_relations": evidence.get("parent_relations", []),
                     "match": evidence["match"], "identity_evidence": identity}]
        if not self.allow_ols:
            return []
        if node["kind"] == "Gene":
            refs = [item for item in node.get("standard_ids", []) if _GENE.fullmatch(item)]
            if len(refs) != 1:
                return []
            accession = refs[0].split(":")[1]
            url = f"https://rest.genenames.org/fetch/entrez_id/{accession}"
            hgnc = _hgnc(url, self._body(url), accession, node)
            log.append({"source": "HGNC nomenclature", "url": url,
                        "status": "observed" if hgnc else "identity_unconfirmed"})
            if not hgnc:
                return []
            source, ident = "HGNC", hgnc["record_id"]
            identity = {"cross_reference": refs[0], **hgnc}
        elif node["kind"] == "Protein":
            grounds = node.get("prior_grounding", [])
            grounded = {ident for row in grounds for ident in row["standard_ids"]}
            refs = {ident for ident in [*grounded, *node["standard_ids"]] if ident.startswith("UniProt:")}
            if len(refs) != 1:
                return []
            identifier, = refs
            source, ident = "PRO", "PR:" + identifier.split(":")[1]
            identity = {"cross_reference": identifier, "grounding": grounds, "kg_record": node.get("kg_record")}
        else:
            return []
        record = self.catalog.inspect_term(source, ident)["record"]
        log.append({"action": "inspect", "source": source, "term_id": ident, "status": "observed",
                    "result": {"record": record}})
        if not record or record["term_id"] != ident:
            return []
        if source == "HGNC":
            if norm(record.get("symbol") or "") != norm(node["name"]):
                return []
            if not record.get("definition"):
                record = {**record, "definition": hgnc["definition"], "definition_source": hgnc["url"]}
        else:
            xref = identifier.replace("UniProt:", "UniProtKB:", 1)
            taxon = str(node.get("taxon_id") or "").removeprefix("NCBITaxon:")
            if xref not in record.get("database_xrefs", []) or taxon not in record.get("taxon_ids", []):
                return []
        return [{**record, "match": "verified_database_cross_reference", "identity_evidence": identity}]

    def resolve(self, node):
        node = deepcopy(node)
        context = self._context(node)
        cache_key = ["grounded-reference-v4", node, context, self.allow_ols, self.obo.metadata if self.obo else None]
        path = self.directory / "definitions" / f"{digest(cache_key)}.json"
        if path.is_file():
            result = read_json(path)
        else:
            evidence, observations = self._lookup(node, context)
            result = {"evidence": evidence, "observations": observations, "original_context": context}
            if evidence:
                try:
                    result["reference_terms"] = self._reference_terms(node, evidence, observations)
                except (OLSError, OSError, ValueError, KeyError, TypeError) as error:
                    observations.append({"status": "reference_lookup_failed", "error": str(error)})
            if not any(row.get("status") in {"reference_lookup_failed", "lookup_failed", "request_or_response_error"}
                       for row in observations):
                atomic_json(path, result)
        if expansion := context.get("attested_expansion"):
            node["aliases"] = sorted({*node["aliases"], expansion})
        if context:
            node["source_context"] = {**node.get("source_context", {}), "literature_context": context}
        if evidence := result["evidence"]:
            node["definition"] = evidence["definition"]
            node["definition_evidence"] = evidence
            if evidence.get("verified_standard_ids"):
                node["standard_ids"] = sorted({*node["standard_ids"], *evidence["verified_standard_ids"]})
            if evidence.get("taxon_id"):
                node["taxon_id"] = evidence["taxon_id"]
            if result.get("reference_terms"):
                node["reference_terms"] = result["reference_terms"]
            if self.allow_ols:
                from .auxiliary import auxiliary_context
                facts, observations = auxiliary_context(node, evidence, self.directory / "raw", self.read_url)
                if facts:
                    node["auxiliary_evidence"] = facts
                if observations:
                    result = {**result, "auxiliary_observations": observations}
        return node, result
