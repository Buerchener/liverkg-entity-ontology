"""Look up candidate entities and direct superclass evidence in public OLS sources."""

from functools import lru_cache
import re
from unicodedata import normalize
from urllib.parse import quote, urlencode
from urllib.request import Request

from .go_reference.ols import (
    EXACT_SYNONYM, IAO_DEFINITION, OBO, OLSError, REST_URL, _json, _read, _values,
)


SOURCES = {
    "Disease": ("mondo", "MONDO", "MONDO"),
    "Protein": ("pr", "PR", "PRO"),
    "Metabolite": ("chebi", "CHEBI", "ChEBI"),
    "Tissue": ("uberon", "UBERON", "Uberon"),
    "CellType": ("cl", "CL", "CL"),
}
MAX_CONCEPTS = 4
MAX_NAMES = 2
SEARCH_RESULTS = 6
MAX_DETAILS = 4
MAX_DETAILS_PER_NAME = 3
MAX_TERM_CANDIDATES = 3
OBO_LOCAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,35}\Z")


def _oboid(iri, prefix):
    start = f"{OBO}{prefix}_"
    if isinstance(iri, str) and iri.startswith(start):
        local = iri[len(start):]
        if OBO_LOCAL.fullmatch(local):
            return f"{prefix}:{local}"
    return None


def _consistent_id(value, term_id, prefix):
    # OLS sometimes uses the OBO short form for PR IDs that contain letters.
    return value == term_id or (prefix == "PR" and value == term_id.replace(":", "_", 1))


def _key(text):
    return " ".join(normalize("NFKC", text).casefold().split())


def _concept_queries(concept):
    queries = {}
    for phrase in [concept["name"], *concept.get("aliases", [])]:
        if 0 < len(phrase.strip()) <= 200:
            queries.setdefault(_key(phrase), phrase.strip())
    return list(queries.values())[:MAX_NAMES]


def _match(query_names, labels, synonyms):
    if any(_key(label) in query_names for label in labels):
        return "exact_label"
    if any(_key(synonym) in query_names for synonym in synonyms):
        return "exact_synonym"
    return None


class OntologyOLS:
    """Bounded REST searches in the ontology selected by the local entity type."""

    def __init__(self, *, timeout=8, get_json=None):
        self.timeout = timeout
        self.get_json = get_json or self._get_json

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
        term_id = _oboid(iri, prefix)
        encoded = quote(quote(iri, safe=""), safe="")
        url = f"{REST_URL}/v2/ontologies/{ontology}/classes/{encoded}"
        row = self._response(url)
        defining = row.get("isDefiningOntology") is True
        if prefix == "PR" and row.get("isDefiningOntology") is False:
            defining = "pr" in _values(row.get("appearsIn"))
        if (row.get("iri") != iri or not _consistent_id(row.get("curie"), term_id, prefix)
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
        return (
            {"term_id": term_id, "name": labels[0], "definition": definitions[0] if definitions else None,
             "iri": iri, "source_url": url},
            labels,
            _values(row.get(EXACT_SYNONYM)),
            set(_values(row.get("directParent"))),
        )

    def _terms(self, concept, ontology, prefix):
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
                    return found, failed
            if found:
                break
        return found, failed

    def lookup(self, concept, questions):
        kind = concept["kind"]
        if kind not in SOURCES:
            return {"status": "NO_MATCH", "source": None, "terms": [], "relations": [],
                    "reason": "unsupported_entity_type"}
        ontology, prefix, source = SOURCES[kind]
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

        rows, failed = {}, False
        for ident, row in selected.items():
            rows[ident], lookup_failed = self._terms(row, ontology, prefix)
            failed = failed or lookup_failed

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
        status = "REFERENCE_ONLY" if terms else ("LOOKUP_FAILED" if failed else "NO_MATCH")
        return {
            "status": status, "source": source, "terms": terms, "relations": list(relations.values()),
            "reason": "incomplete_lookup" if failed else (
                "term_candidates_for_review" if terms else "no_exact_label_or_synonym"
            ),
        }
