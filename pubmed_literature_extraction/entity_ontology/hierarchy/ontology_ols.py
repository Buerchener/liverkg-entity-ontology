"""Look up candidate entities and direct superclass evidence in public OLS sources."""

from functools import lru_cache
import re
from unicodedata import normalize
from urllib.parse import quote, urlencode
from urllib.request import Request

from .ols_client import (
    EXACT_SYNONYM, IAO_DEFINITION, OBO, OBO_IN_OWL, OLSClient, OLSError, REST_URL, _json, _read, _values,
)


OLS_SOURCES = {
    "HGNC": ("hgnc", "HGNC"),
    "MONDO": ("mondo", "MONDO"),
    "PRO": ("pr", "PR"),
    "ChEBI": ("chebi", "CHEBI"),
    "Uberon": ("uberon", "UBERON"),
    "CL": ("cl", "CL"),
    "GO": ("go", "GO"),
}
KIND_SOURCES = {
    "Gene": "HGNC",
    "Disease": "MONDO",
    "Protein": "PRO",
    "Metabolite": "ChEBI",
    "Tissue": "Uberon",
    "CellType": "CL",
}
MAX_CONCEPTS = 4
MAX_NAMES = 2
SEARCH_RESULTS = 6
MAX_DETAILS = 4
MAX_DETAILS_PER_NAME = 3
MAX_TERM_CANDIDATES = 3
OBO_LOCAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,35}\Z")
GO_ID = re.compile(r"GO:[0-9]{7}\Z")
HGNC_REPORT = "https://www.genenames.org/data/gene-symbol-report/#!/hgnc_id/"
HGNC_LOCAL = re.compile(r"[1-9][0-9]*")
HGNC_NAME_RESULTS = 16
HUMAN_PREFIX = re.compile(r"^(?:human|homo sapiens)\s+", re.I)
NONHUMAN_PREFIX = re.compile(
    r"^(?:non[- ]human|mouse|murine|mus musculus|rat|rattus norvegicus|"
    r"zebrafish|danio rerio|drosophila)\s+", re.I
)
HGNC_ALIAS_TYPES = tuple(OBO + code for code in ("OMO_0003015", "OMO_0003016", "OMO_0003008"))


def _oboid(iri, prefix):
    start, pattern = (HGNC_REPORT, HGNC_LOCAL) if prefix == "HGNC" else (f"{OBO}{prefix}_", OBO_LOCAL)
    if isinstance(iri, str) and iri.startswith(start):
        local = iri[len(start):]
        if pattern.fullmatch(local):
            return f"{prefix}:{local}"
    return None


def _consistent_id(value, term_id, prefix):
    # OLS sometimes uses the OBO short form for PR IDs that contain letters.
    return (value == term_id
            or (prefix == "PR" and value == term_id.replace(":", "_", 1))
            or (prefix == "HGNC" and value == term_id.lower()))


def _key(text):
    return " ".join(normalize("NFKC", text).casefold().split())


def _concept_queries(concept, *, human_gene=False):
    queries = {}
    for phrase in [concept["name"], *concept.get("aliases", [])]:
        if human_gene:
            phrase = " ".join(phrase.split())
            if NONHUMAN_PREFIX.match(phrase):
                continue
            phrase = re.sub(r"\s+gene$", "", HUMAN_PREFIX.sub("", phrase), flags=re.I)
        if 0 < len(phrase.strip()) <= 200:
            queries.setdefault(_key(phrase), phrase.strip())
    return list(queries.values())[:MAX_NAMES]


def _match(query_names, labels, synonyms):
    if any(_key(label) in query_names for label in labels):
        return "exact_label"
    if any(_key(synonym) in query_names for synonym in synonyms):
        return "exact_synonym"
    return None


def _gene_aliases(row):
    # Untyped HGNC synonyms can name related diseases.
    records = row.get("synonym", [])
    aliases = []
    if not isinstance(records, list):
        return aliases
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("axioms"), list):
            continue
        if any(
            axiom.get(OBO_IN_OWL + "hasSynonymType") in HGNC_ALIAS_TYPES
            for axiom in record["axioms"] if isinstance(axiom, dict)
        ):
            aliases.extend(_values(record))
    return aliases


class OntologyOLS:
    """Search OLS and verify official term records in a specified ontology."""

    def __init__(self, *, timeout=8, get_json=None, mcp=None):
        self.timeout = timeout
        self.get_json = get_json or self._get_json
        self.mcp = mcp if mcp is not None else OLSClient(timeout=timeout)

    def search_source(self, source, query):
        """Discovery hits are leads. Only verified records can support a local pair."""
        ontology, prefix = OLS_SOURCES[source]
        try:
            rows = self.mcp.search_classes(ontology, query)
        except OLSError:
            rows = self._search(ontology, prefix, query)
            transport = "rest"
        else:
            rows = [row for row in rows if isinstance(row, dict) and row.get("ontologyId") == ontology
                    and row.get("isObsolete") is False and "class" in _values(row.get("type"))]
            transport = "mcp"
        hits = []
        for row in rows:
            term_id = _oboid(row.get("iri"), prefix)
            labels = _values(row.get("label"))
            if term_id and labels:
                hits.append({"term_id": term_id, "label": labels[0]})
        return {"transport": transport, "hits": hits}

    def inspect_term(self, source, term_id):
        ontology, prefix = OLS_SOURCES[source]
        if not term_id.startswith(prefix + ":"):
            raise ValueError("Term ID does not belong to the chosen source")
        local = term_id.split(":", 1)[1]
        iri = HGNC_REPORT + local if prefix == "HGNC" else OBO + prefix + "_" + local
        if _oboid(iri, prefix) != term_id:
            raise ValueError("Invalid term ID")
        detail = self._detail(ontology, prefix, iri)
        if not detail:
            return {"record": None}
        term, _, synonyms, parents = detail
        return {"record": {**term, "synonyms": synonyms[:12],
                           "direct_parent_ids": self._parent_ids(parents, prefix)}}

    @staticmethod
    def _parent_ids(parents, prefix):
        return sorted({ident for iri in parents if (ident := _oboid(iri, prefix))})

    def verify_endpoint(self, source, concept, *, name=None):
        """Verify an endpoint against official records and return its direct superclass IDs."""
        ontology, prefix = OLS_SOURCES[source]
        if (source == "HGNC") != (concept["kind"] == "Gene"):
            return {"terms": [], "reason": "gene_identity_requires_hgnc"}
        if source == "GO" and concept.get("go_id") and GO_ID.fullmatch(concept["go_id"]):
            detail = self._detail(ontology, prefix, OBO + concept["go_id"].replace(":", "_"))
            records = [] if detail is None else [({**detail[0], "concept_id": concept["id"],
                                                    "match": "supplied_id"}, detail[3])]
            issue = None
        else:
            aliases = [concept["name"], *concept.get("aliases", [])]
            if name:
                if name not in aliases:
                    raise ValueError("Verification requires a name or alias supplied for this endpoint")
                # Retain the original name for the HGNC species check.
                if NONHUMAN_PREFIX.match(concept["name"].strip()) and source == "HGNC":
                    return {"terms": [], "reason": "UNCERTAIN"}
                concept = {**concept, "name": name, "aliases": [label for label in aliases if label != name]}
            records, issue = self._terms(concept, ontology, prefix)
        terms = [{**term, "source": source, "direct_parent_ids": self._parent_ids(parents, prefix)}
                 for term, parents in records if source != "GO" or term.get("definition")]
        return {"terms": terms if not issue else [], "reason": issue}

    def _get_json(self, url):
        _, response = _read(Request(url, headers={"Accept": "application/json"}), self.timeout)
        return _json(response)

    @lru_cache(maxsize=512)
    def _response(self, url):
        data = self.get_json(url)
        if not isinstance(data, dict):
            raise OLSError("Expected an OLS JSON object")
        return data

    def _search(self, ontology, prefix, query):
        params = {
            "ontology": ontology, "type": "class", "obsoletes": "false", "rows": SEARCH_RESULTS,
            "queryFields": "label,synonym", "exact": "true", "q": query,
            "fieldList": "iri,label,synonym,obo_id,ontology_name,type,is_obsolete",
        }
        # At present OLS does not mark classes in the PR namespace as
        # isDefiningOntology. Verify their OBO namespace and PR detail below.
        if prefix != "PR":
            params["local"] = "true"
        url = f"{REST_URL}/search?{urlencode(params)}"
        response = self._response(url).get("response")
        if not isinstance(response, dict) or not isinstance(response.get("docs"), list):
            raise OLSError("Invalid OLS search rows")
        if prefix == "HGNC" and response.get("numFound") != len(response["docs"]):
            raise OLSError("Incomplete OLS HGNC search rows")
        hits = {}
        for hit in response["docs"][:SEARCH_RESULTS]:
            if not isinstance(hit, dict) or hit.get("ontology_name") != ontology:
                continue
            iri = hit.get("iri")
            term_id = _oboid(iri, prefix)
            if (term_id and _consistent_id(hit.get("obo_id"), term_id, prefix)
                    and hit.get("type") == "class" and hit.get("is_obsolete") is not True):
                hits.setdefault(iri, hit)
        return sorted(hits.values(), key=lambda row: not _match(
            {_key(query)}, _values(row.get("label")), _values(row.get("synonym")),
        ))

    def _detail(self, ontology, prefix, iri):
        encoded = quote(quote(iri, safe=""), safe="")
        url = f"{REST_URL}/v2/ontologies/{ontology}/classes/{encoded}"
        return self._verified_detail(ontology, prefix, iri, self._response(url), url)

    def _verified_detail(self, ontology, prefix, iri, row, url):
        term_id = _oboid(iri, prefix)
        defining = row.get("isDefiningOntology") is True
        if prefix == "PR" and row.get("isDefiningOntology") is False:
            defining = "pr" in _values(row.get("appearsIn"))
        if (not term_id or row.get("iri") != iri or not _consistent_id(row.get("curie"), term_id, prefix)
                or row.get("ontologyId") != ontology or row.get("isObsolete") is not False
                or not defining or "class" not in _values(row.get("type"))):
            return None
        labels = _values(row.get("label"))
        if not labels:
            raise OLSError("Verified OLS class has no label")
        definitions = _values(row.get(IAO_DEFINITION)) or [
            definition for definition in _values(row.get("definition"))
            if not definition.startswith("Category=")
        ]
        if prefix == "HGNC":
            # The HGNC index puts the official gene name in its definition field.
            name = definitions[0] if definitions else labels[0]
            return (
                {"term_id": term_id, "name": name, "symbol": labels[0], "definition": None,
                 "organism": "Homo sapiens", "iri": iri, "source_url": url},
                [*labels, name], _gene_aliases(row), set(),
            )
        return (
            {"term_id": term_id, "name": labels[0], "definition": definitions[0] if definitions else None,
             "iri": iri, "source_url": url},
            labels,
            _values(row.get(EXACT_SYNONYM)),
            set(_values(row.get("directParent"))),
        )

    def _gene_name_details(self, ontology, prefix, query):
        params = urlencode({"search": query, "size": HGNC_NAME_RESULTS})
        url = f"{REST_URL}/v2/ontologies/{ontology}/classes?{params}"
        response = self._response(url)
        rows = response.get("elements")
        total = response.get("totalElements")
        if not isinstance(rows, list) or type(total) is not int or total < len(rows):
            raise OLSError("Invalid OLS HGNC name search")
        if total > len(rows):
            return None
        return [self._verified_detail(ontology, prefix, row.get("iri"), row, url)
                for row in rows if isinstance(row, dict)]

    def _gene_terms(self, concept, ontology, prefix):
        taxon = str(concept.get("taxon_id", "9606")).removeprefix("NCBITaxon:")
        if taxon != "9606" or NONHUMAN_PREFIX.match(concept["name"].strip()):
            return [], "UNCERTAIN"
        for query in _concept_queries(concept, human_gene=True):
            try:
                hits = self._search(ontology, prefix, query)
                if len(hits) > MAX_DETAILS_PER_NAME:
                    return [], "UNCERTAIN"
                details = [self._detail(ontology, prefix, hit["iri"]) for hit in hits]
                if not hits and " " in query:
                    details = self._gene_name_details(ontology, prefix, query)
                    if details is None:
                        return [], "UNCERTAIN"
            except OLSError:
                return [], "LOOKUP_FAILED"

            matches = {}
            for detail in details:
                if detail is None:
                    continue
                term, labels, synonyms, parents = detail
                match = _match({_key(query)}, labels, synonyms)
                if match is not None:
                    matches[term["term_id"]] = ({**term, "concept_id": concept["id"], "match": match}, parents)
            if len(matches) > 1:
                return [], "UNCERTAIN"
            if matches:
                return list(matches.values()), None
        return [], None

    def _terms(self, concept, ontology, prefix):
        if prefix == "HGNC":
            return self._gene_terms(concept, ontology, prefix)
        queries = _concept_queries(concept)
        names = {_key(query) for query in queries}
        visited, found, failed = set(), [], False
        for query in queries:
            try:
                hits = self._search(ontology, prefix, query)
            except OLSError:
                failed = True
                continue
            unvisited = [hit for hit in hits if hit["iri"] not in visited]
            for hit in unvisited[:MAX_DETAILS_PER_NAME]:
                iri = hit["iri"]
                visited.add(iri)
                try:
                    detail = self._detail(ontology, prefix, iri)
                except OLSError:
                    detail, failed = None, True
                if detail is not None:
                    term, labels, synonyms, parent_iris = detail
                    match = _match(names, labels, synonyms)
                    if match is not None:
                        found.append(({**term, "concept_id": concept["id"], "match": match}, parent_iris))
                if len(visited) >= MAX_DETAILS or len(found) >= MAX_TERM_CANDIDATES:
                    return found, "LOOKUP_FAILED" if failed else None
            if found:
                break
        return found, "LOOKUP_FAILED" if failed else None

    def lookup(self, concept, questions):
        kind = concept["kind"]
        source = KIND_SOURCES.get(kind)
        if source is None:
            return {"status": "NO_MATCH", "source": None, "terms": [], "relations": [],
                    "reason": "unsupported_entity_type"}
        ontology, prefix = OLS_SOURCES[source]
        selected = {concept["id"]: concept}
        pairs = set()
        for question in questions:
            child, parent = question["child"], question["parent"]
            if (child["kind"] != kind or parent["kind"] != kind
                    or concept["id"] not in (child["id"], parent["id"])):
                continue
            for endpoint in (child, parent):
                if len(selected) < MAX_CONCEPTS:
                    selected.setdefault(endpoint["id"], endpoint)
            if child["id"] in selected and parent["id"] in selected:
                pairs.add((child["id"], parent["id"]))
                pairs.add((parent["id"], child["id"]))

        rows, issues = {}, set()
        for ident, row in selected.items():
            rows[ident], issue = self._terms(row, ontology, prefix)
            if issue:
                issues.add(issue)

        terms = [term for matched in rows.values() for term, _ in matched]
        relations = {}
        for child, parent in sorted(pairs):
            for current, parent_iris in rows.get(child, []):
                for candidate, _ in rows.get(parent, []):
                    edge = (current["term_id"], candidate["term_id"])
                    if edge[0] != edge[1] and candidate["iri"] in parent_iris:
                        relations[edge] = {
                            "child_id": edge[0], "parent_id": edge[1],
                            "predicate": "is_a", "source_url": current["source_url"],
                        }
        if terms:
            status, reason = "REFERENCE_ONLY", "term_candidates_for_review"
        elif "LOOKUP_FAILED" in issues:
            status, reason = "LOOKUP_FAILED", "incomplete_lookup"
        elif "UNCERTAIN" in issues:
            status, reason = "UNCERTAIN", "no_unique_human_gene_reference"
        else:
            status, reason = "NO_MATCH", "no_exact_label_or_synonym"
        return {
            "status": status, "source": source, "terms": terms, "relations": list(relations.values()),
            "reason": "incomplete_lookup" if "LOOKUP_FAILED" in issues else reason,
        }
