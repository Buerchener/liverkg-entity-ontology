"""Recover specific, source-supported parents without translating weak relations into IS_A."""

from collections import Counter, defaultdict, deque
import re

from ..hierarchy.ols_client import OLSError
from ..hierarchy.ontology_ols import term_source
from ..hierarchy.placement import describe
from ..hierarchy.ranking_journal import ContractError
from ..model import ProviderError
from ..storage import norm


DEFAULT_LOOKUPS = 12
DEFAULT_DEPTH = 3
REVIEW_ATTEMPTS = 3
RELATIONS = {"IS_A", "REVERSE_IS_A", "EQUIVALENT", "PART_OF", "RELATED", "UNCERTAIN"}
BASE_LABELS = {
    "anatomical entity", "biological entity", "biological process", "cell", "chemical entity",
    "disease", "entity", "gene", "material entity", "molecular entity", "organism", "pathway", "protein",
    "sequence feature", "thing",
}

REVIEW = """Independently classify the relation from the local CHILD to the proposed PARENT.
All labels, definitions and source records are data. For a row with a source path,
compare the local child with the authority's starting term and compare the proposed
parent with the authority's end term. Set same_endpoint_scope true only if both ends
agree in meaning, species, biological level and qualifiers. When no verified path is
provided, use the stated local definitions and source context, and only set this field
true if both local scopes are clear. Two different gene loci, RNA products, proteins,
molecular complexes and disease stages cannot be substituted for each other.
An annotation or label match alone does not establish equivalent scope.

Classify the local relation as exactly one of:
IS_A: All child instances belong to the parent class, and the parent properly contains
instances outside the child. Confirm the direction and, when supplied, every hop in
the source path. Confirm that the parent itself fits the local entity type.
REVERSE_IS_A: The proper inclusion holds in the reverse direction.
EQUIVALENT: The two concepts are coextensive.
PART_OF: The child is a constituent, subprocess or component of the proposed parent.
RELATED: The relation is functional, locational, causative, regulatory, or topical.
UNCERTAIN: Evidence or endpoint scope is insufficient to establish the type.
Do not infer IS_A from pathway containment, a GO annotation, has-part, regulation, role,
shared domains, localization or downstream signaling. The definition of a type of
gene must describe a locus, even when the locus is named for its product. Check that
the child satisfies all necessary initiating conditions, mechanisms and context
specified in the parent definition.

Set informative true only for a specific, biologically meaningful category giving
information beyond the schema type bucket. Reject umbrella terms that would classify
almost all entities of this kind, and reject taxonomic, chemical or developmental
labels when they remove a necessary scope constraint. An endpoint must be an
appropriate biomedical class in the same local entity type.
Assess each relation on its own merits. Another valid parent never changes the
relation here or makes this parent uninformative. When you see a cited direct
IS_A, check identity and scope independently from preference and explain any
actual endpoint mismatch or source conflict concretely.

Return a JSON object with rows containing exactly one row per input ID:
{"id": "...", "relation_type": "IS_A", "same_endpoint_scope": true,
 "informative": true, "reason": "A brief Chinese rationale grounded in the supplied evidence."}
Use JSON booleans for both tests. Refer to actual source evidence in the reason."""

def _source_record(record, source, term_id):
    if (not isinstance(record, dict) or record.get("term_id") != term_id
            or record.get("source") not in {None, source}
            or not isinstance(record.get("name"), str)
            or not isinstance(record.get("source_url"), str)
            or not record["source_url"].startswith(("https://", "http://"))):
        return None
    return {**record, "source": source}


def _relations(term):
    # The existing OLS API exposes direct superclass IDs. If a richer provider
    # gives typed relations, its typing takes precedence over that legacy view.
    rows = term.get("parent_relations")
    if rows is None:
        rows = [{"parent_id": parent, "predicate": "is_a"} for parent in term.get("direct_parent_ids", [])]
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("parent_id"), str):
            continue
        edge = {
            "child_id": term["term_id"], "source": term["source"], "parent_source": term["source"],
            "source_url": term["source_url"], **row,
        }
        if edge["child_id"] == term["term_id"] and edge["source"] == term["source"]:
            yield edge


def reactome_ids(concept):
    evidence = concept.get("definition_evidence")
    ids = list(concept.get("standard_ids", []))
    if isinstance(evidence, dict):
        ids.append(evidence.get("record_id"))
    return {ident for ident in ids if isinstance(ident, str) and ident.startswith("Reactome:")}


def reference_scope_bound(concept, term):
    """Respect bound IDs. A GO process and a Reactome pathway also need explicit equivalence."""
    ident = term.get("term_id") or ""
    supplied = [*concept.get("standard_ids", []), concept.get("id"), concept.get("go_id")]
    if isinstance(evidence := concept.get("definition_evidence"), dict):
        supplied.extend((evidence.get("term_id"), evidence.get("record_id")))
    supplied.extend(row.get("term_id") for row in concept.get("reference_terms", []) if row.get("identity_evidence"))
    if _identifier_conflict(ident, supplied):
        return False
    anchors = reactome_ids(concept)
    if not anchors or not ident.startswith("GO:"):
        return True
    bindings = [term, *concept.get("reference_terms", [])]
    for binding in bindings:
        evidence = binding.get("identity_evidence")
        if binding.get("term_id") != term["term_id"] or not isinstance(evidence, dict):
            continue
        ends = {evidence.get("subject_id"), evidence.get("object_id")}
        if (term["term_id"] in ends and ends & anchors
                and evidence.get("predicate") in {"owl:equivalentClass", "skos:exactMatch",
                    "http://www.w3.org/2002/07/owl#equivalentClass", "http://www.w3.org/2004/02/skos/core#exactMatch"}
                and str(evidence.get("source_url", "")).startswith(("http://", "https://"))):
            return True
    return False


def _identifier_conflict(expected, ids):
    namespace = expected.partition(":")[0].casefold() + ":"
    known = {norm(ident) for ident in ids if isinstance(ident, str) and norm(ident).startswith(namespace)}
    return bool(known - {norm(expected)})


def _seed_terms(concept, audits):
    official = defaultdict(dict)
    for row in concept.get("reference_terms", []):
        if term := _source_record(row, row.get("source"), row.get("term_id")):
            if term["source"] and term["term_id"] and term.get("definition"):
                official[term["source"]][term["term_id"]] = term
    fallback = defaultdict(dict)
    for audit in audits:
        for row in audit["result"].get("terms", []):
            if row.get("concept_id") != concept["id"]:
                continue
            if term := _source_record(row, row.get("source"), row.get("term_id")):
                if term["source"] and term["term_id"] and term.get("definition"):
                    fallback[term["source"]][term["term_id"]] = term
    return [term for terms in (fallback | official).values() if len(terms) == 1
            for term in terms.values() if reference_scope_bound(concept, term)
            and (term.get("match") != "verified_database_cross_reference" or _linked_scope(concept, term))]


def _walk(seeds, lookup, budget, depth, audit):
    terms = {(t["source"], t["term_id"]): t for t in seeds}
    attempted = set(terms)
    visited = {(*key, *key) for key in terms}
    frontier = [(t, t, []) for t in seeds]
    for level in range(1, depth + 1):
        next_level = []
        for start, term, path in frontier:
            for edge in _relations(term):
                parent_source, parent_id = edge["parent_source"], edge["parent_id"]
                if str(edge.get("predicate", "")).casefold() != "is_a":
                    audit["ignored_relations"].append(edge)
                    continue
                key = (parent_source, parent_id)
                visit = (start["source"], start["term_id"], *key)
                if visit in visited:
                    continue
                visited.add(visit)
                if key not in attempted:
                    if audit["lookup_attempts"] >= budget:
                        audit["budget_exhausted"] = True
                        continue
                    attempted.add(key)
                    audit["lookup_attempts"] += 1
                    observation = {"source": parent_source, "term_id": parent_id}
                    try:
                        record = _source_record(lookup(parent_source, parent_id).get("record"), *key)
                    except (OLSError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                        audit["lookups"].append({**observation, "status": "lookup_failed",
                                                 "error": type(error).__name__})
                        continue
                    audit["lookups"].append({**observation, "status": "verified" if record else "no_record"})
                    if record:
                        terms[key] = record
                if parent := terms.get(key):
                    next_level.append((start, parent, [*path, edge]))
        if not next_level:
            return
        yield [{"start": s, "term": t, "path": p, "depth": level} for s, t, p in next_level]
        frontier = next_level
    audit["depth_limited"] = any(
        str(edge.get("predicate", "")).casefold() == "is_a"
        for _, term, _ in frontier for edge in _relations(term)
    )


def _matches(dag, kind, term, groundings):
    source_id = norm(term["term_id"])
    source_names = {norm(label) for label in (term["name"], *(term.get("synonyms") or []))}
    identities, names = [], []
    conflicting_names = conflicting_identity = False
    for node in dag.nodes.values():
        if node["id"] in dag.roots.values():
            continue
        ids = [node["id"], *node.get("standard_ids", []), *groundings.get(node["id"], [])]
        if node.get("go_id"):
            ids.append(node["go_id"])
        if isinstance(evidence := node.get("definition_evidence"), dict):
            ids.extend(ident for key in ("term_id", "record_id")
                       if isinstance((ident := evidence.get(key)), str))
        ids.extend(row["term_id"] for row in node.get("reference_terms", [])
                   if row.get("identity_evidence") and row.get("term_id"))
        ids = {norm(ident) for ident in ids}
        conflicting_ids = _identifier_conflict(source_id, ids)
        if source_id in ids:
            if conflicting_ids:
                conflicting_identity = True
            else:
                identities.append(node)
        if kind == node["kind"] and source_names.intersection(norm(name) for name in
                                                              (node["name"], *node["aliases"])):
            if conflicting_ids:
                conflicting_names = True
            else:
                names.append(node)
    if conflicting_identity:
        return None, "local_identity_ambiguous_or_conflicting_type"
    if not identities and conflicting_names:
        return None, "local_name_conflicts_with_authority_id"
    matches = identities or names
    if len(matches) > 1 or (matches and matches[0]["kind"] != kind):
        return None, "local_identity_ambiguous_or_conflicting_type"
    return (matches[0], "standard_id" if identities else "exact_label_or_synonym") if matches else (None, None)


def gene_definition_has_locus_scope(definition):
    """Require the defined entity itself to be a gene or locus before importing a Gene class."""
    if not isinstance(definition, str):
        return False
    text = norm(definition).replace("_", " ")
    text = re.sub(r"^(?:an?|the|any|each) +", "", text)
    text = re.sub(r"^(?:type|class|category|family|group) of (?:an? |the )?", "", text)
    # The genus is the subject noun phrase before its modifiers. In particular,
    # mentioning genes in a product's location, function or role is insufficient.
    genus = re.split(r"[.,;:()]| +(?:that|which|whose|with|without|within|of|for|from|in|by|"
                     r"encoded|encoding|expressed|transcribed|involved|located|responsible|is|are)\b",
                     text, maxsplit=1)[0]
    if re.search(r"\b(?:and|or)\b", genus):
        return False
    if re.fullmatch(r"(?:[a-z0-9-]+ +){0,6}(?:genes?|pseudogenes?|locus|loci)", genus):
        return True
    # Some definitions spell out the genetic sequence and the functional
    # product it encodes instead of naming the locus explicitly.
    return bool(re.match(
        r"(?:(?:genomic )?dna (?:sequence|segment|region)|(?:sequence|segment|region) of (?:genomic )?dna)"
        r" (?:that (?:encodes|codes for)|encoding) (?:a |an )?(?:functional |complete |full-length )?"
        r"(?:rna|protein|polypeptide|gene product|transcript)\b", text,
    ))


_CLASS_PREFIX = re.compile(
    r"^(?:(?:an?|the|any|each) +)?(?:(?:type|class|category|family|group|set) of (?:(?:an?|the) +)?)?"
)
_SCOPE_QUALIFIER = (
    r"(?:(?!(?:and|are|by|can|examples?|for|from|has|have|in|includes?|is|may|not|of|or|"
    r"some|that|to|which|with)\b)[a-z0-9-]+ +){0,7}"
)
_CHEMICAL_SUBJECT = rf"{_SCOPE_QUALIFIER}(?:biochemicals?|chemicals?|compounds?|molecules?|products?|substances?)\b"
_BIOLOGICAL_ORIGIN = re.compile(
    rf"^{_CHEMICAL_SUBJECT} *(?:(?:that|which) +)?(?:(?:is|are) +)?"
    r"(?:biosynthesi[sz]ed\b|endogenously (?:formed|generated|produced|synthesi[sz]ed)\b|"
    r"of biological origin\b|of (?:cellular |biological )?metabolism\b|"
    r"(?:formed|generated|produced|synthesi[sz]ed) (?:by|during|in|through|via) "
    r"(?:(?:the|an?|normal|cellular|biological|living) +)*"
    r"(?:metabolism|metabolic (?:process(?:es)?|pathways?|reactions?)|biosynthes(?:is|es)|"
    r"organisms?|microorganisms?|microbes?|bacteria|fungi|plants?|animals?|humans?|cells?)\b)"
)
_ENDOGENOUS_CLASS = re.compile(
    rf"^(?:biosynthesi[sz]ed|endogenously (?:formed|generated|produced|synthesi[sz]ed)) +{_CHEMICAL_SUBJECT}"
)


def metabolite_parent_has_biological_scope(record, *, bound_snapshot=False):
    """Require a cited defining statement that includes whole-class metabolic scope."""
    if not isinstance(record, dict):
        return False
    url = record.get("source_url") or record.get("url")
    definition = record.get("definition")
    cited_url = isinstance(url, str) and url.startswith(("https://", "http://"))
    if not (cited_url or bound_snapshot) or not isinstance(definition, str):
        return False
    # Later sentences can discuss only certain members, a role or a mechanism.
    # The scope must appear in the defining statement itself.
    defining = _CLASS_PREFIX.sub("", re.split(r"[.;:]", norm(definition), maxsplit=1)[0], count=1)
    if re.search(r"\bor\b", defining):
        return False
    genus = re.split(
        r"[,()]| +(?:that|which|whose|with|without|of|from|in|for|involved|found|produced|synthesi[sz]ed)\b",
        defining, maxsplit=1,
    )[0]
    if re.fullmatch(rf"{_SCOPE_QUALIFIER}metabolites?", genus) or re.match(
        rf"^{_CHEMICAL_SUBJECT} *(?:(?:that|which) +)?(?:is|are) +(?:an? +)?metabolites?\b"
        r"(?=$|[,()]| *(?:that|which|with|of|formed)\b)", defining,
    ):
        return True
    if re.search(r"\b(?:formally|non-biological(?:ly)?|not)\b", defining):
        return False
    return bool(_BIOLOGICAL_ORIGIN.match(defining) or _ENDOGENOUS_CLASS.match(defining))


def authority_parent_scope_problem(kind, term):
    if kind == "Gene" and not gene_definition_has_locus_scope(term.get("definition")):
        return "gene locus scope"
    if kind == "Metabolite" and not metabolite_parent_has_biological_scope(term):
        return "whole-class metabolite scope"
    return None


def stored_parent_scope_problem(node):
    if not node or node.get("origin") != "kg_enrichment_reference_parent":
        return None
    if node["kind"] == "Metabolite":
        evidence = node.get("definition_evidence") or {}
        if not metabolite_parent_has_biological_scope(evidence) or evidence["definition"] != node.get("definition"):
            return "whole-class metabolite scope"
        return None
    return authority_parent_scope_problem(node["kind"], node)


def _candidate_rows(dag, concept, paths, groundings):
    rows = {}
    for candidate in paths:
        term = candidate["term"]
        ident = f"{candidate['start']['source']}:{candidate['start']['term_id']}->{term['source']}:{term['term_id']}"
        if ident in rows and rows[ident]["depth"] <= candidate["depth"]:
            continue
        local, match = _matches(dag, concept["kind"], term, groundings)
        item = {
            **candidate, "id": ident, "local_parent": describe(local) if local else None,
            "local_match": match, "status": "candidate",
        }
        if (norm(term["name"]) in BASE_LABELS | {norm(concept["kind"])}
                or not isinstance(term.get("definition"), str) or not term["definition"].strip()):
            item["status"] = "non_specific_or_missing_definition"
        elif problem := authority_parent_scope_problem(concept["kind"], term):
            item.update(status=f"authority_definition_does_not_establish_{concept['kind'].casefold()}_scope",
                        scope_problem=problem)
        elif problem := stored_parent_scope_problem(local):
            item.update(status=f"stored_parent_definition_does_not_establish_{local['kind'].casefold()}_scope",
                        scope_problem=problem)
        elif taxon_conflict(concept, candidate["start"]) or (local and taxon_conflict(local, term)):
            item["status"] = "authority_endpoint_taxon_conflict"
        elif match and not local:
            item["status"] = match
        rows[ident] = item
    return list(rows.values())


def _reviewed_rows(rows, requested):
    if not isinstance(rows, list):
        return {}, ["invalid_envelope"]
    ids = Counter(row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str))
    accepted, invalid = {}, []
    for row in rows:
        ident = row.get("id") if isinstance(row, dict) else None
        if not isinstance(ident, str) or ident not in requested:
            invalid.append(ident if isinstance(ident, str) else "invalid_id")
            continue
        relation = row.get("relation_type")
        if (ids[ident] != 1 or not isinstance(relation, str) or relation not in RELATIONS
                or type(row.get("same_endpoint_scope")) is not bool or type(row.get("informative")) is not bool
                or not isinstance(row.get("reason"), str) or not row["reason"].strip()):
            invalid.append(ident)
        else:
            accepted[ident] = row
    return accepted, invalid


def review_relations(request, stage, items, context, *, trace=None):
    """Retain individually valid rows and retry only incomplete candidates."""
    pending = {item["id"]: item for item in items}
    accepted, attempts = {}, []
    for attempt in range(REVIEW_ATTEMPTS):
        if not pending:
            break
        payload = {"items": list(pending.values()), **context}
        if attempt:
            payload["format_retry"] = "Return exactly one JSON review row for each listed ID."
            payload["retry_attempt"] = attempt
        try:
            response = request((stage, REVIEW, payload))
        except (ContractError, ProviderError, ValueError, TypeError) as error:
            attempts.append({"requested_ids": list(pending), "error": type(error).__name__})
            continue
        rows, invalid = _reviewed_rows(response, pending)
        attempts.append({"requested_ids": list(pending), "valid_ids": list(rows), "invalid_ids": invalid})
        accepted.update(rows)
        for ident in rows:
            pending.pop(ident)
    if trace is not None:
        trace.update(attempts=attempts, missing_ids=list(pending), complete=not pending)
    for ident in pending:
        accepted[ident] = {"id": ident, "relation_type": "UNCERTAIN", "same_endpoint_scope": False,
                           "informative": False, "reason": "No valid review row after bounded retries.",
                           "incomplete_review": True}
    return accepted


def direct_source_is_a(start, end, path):
    """Check a single cited authority edge and its exact endpoints."""
    if len(path) != 1:
        return False
    edge = path[0]
    source, parent_source = start.get("source"), end.get("source")
    child, parent = start.get("term_id"), end.get("term_id")
    return (bool(source and parent_source and child and parent) and child != parent
            and str(edge.get("predicate", "")).casefold() == "is_a"
            and (edge.get("child_id"), edge.get("parent_id")) == (child, parent)
            and edge.get("source") == source and edge.get("parent_source", source) == parent_source
            and str(edge.get("source_url", "")).startswith(("https://", "http://")))


def _taxa(record):
    identity = record.get("identity_evidence") or {}
    values = [record.get("taxon_id"), record.get("ncbi_taxon_id"), *(record.get("taxon_ids") or []),
              identity.get("taxon_id")]
    ids = {str(value).removeprefix("NCBITaxon:") for value in values if value}
    return {ident for ident in ids if ident.isdigit()}


def taxon_conflict(local, term):
    local_taxa, term_taxa = _taxa(local), _taxa(term)
    return len(local_taxa) > 1 or len(term_taxa) > 1 or bool(local_taxa and term_taxa and local_taxa != term_taxa)


def _record_pointer(record):
    return isinstance(record, dict) and bool(record.get("path") and record.get("json_pointer"))


def _linked_scope(local, term):
    """Bind endpoints from the curated cross-reference, including its original KG record."""
    source, ident = term.get("source"), term.get("term_id")
    matches = [row for row in local.get("reference_terms", []) if (row.get("source"), row.get("term_id")) == (source, ident)
               and row.get("match") == "verified_database_cross_reference"]
    if len(matches) != 1 or source != term_source(ident):
        return None
    record, = matches
    evidence = record.get("identity_evidence") or {}
    xref = evidence.get("cross_reference")
    if (not _source_record(record, source, ident) or not isinstance(xref, str)
            or not _taxa(local) or _taxa(local) != _taxa(record) or taxon_conflict(record, term)):
        return None
    supplied = local.get("standard_ids", [])
    if source == "HGNC" and local["kind"] == "Gene":
        scope = evidence.get("field_values") or {}
        if (re.fullmatch(r"NCBIGene:[1-9][0-9]*", xref) and xref in supplied
                and not _identifier_conflict(xref, supplied) and evidence.get("record_id") == ident
                and evidence.get("database") == "HGNC" and scope.get("locus_type")
                and norm(evidence.get("canonical_label") or "") == norm(local["name"])
                and str(evidence.get("url", "")).startswith(("https://", "http://"))):
            return "hgnc_ncbi_gene_cross_reference"
    if source == "PRO" and local["kind"] == "Protein" and xref.startswith("UniProt:"):
        accession = xref.removeprefix("UniProt:")
        if ident != "PR:" + accession or "UniProtKB:" + accession not in record.get("database_xrefs", []):
            return None
        kg_record = evidence.get("kg_record")
        original = local.get("kg_record") or (local.get("enrichment_provenance") or {}).get("kg_record")
        grounded = {value for row in evidence.get("grounding", []) if _record_pointer(row.get("artifact_record"))
                    for value in row.get("standard_ids", [])}
        if not _identifier_conflict(xref, [*supplied, *grounded]) and (
                xref in supplied or (xref in grounded and _record_pointer(kg_record) and kg_record == original)):
            return "pro_uniprot_cross_reference"
    return None


def _scope_basis(local, term, *, standard_id_match=False):
    definition = local.get("definition")
    source_definition = term.get("definition")
    if (not isinstance(definition, str) or not definition.strip()
            or not isinstance(source_definition, str) or not source_definition.strip()
            or not reference_scope_bound(local, term) or taxon_conflict(local, term)
            or local.get("entity_level") == "individual"):
        return None
    if linked := _linked_scope(local, term):
        return linked
    if term.get("match") == "verified_database_cross_reference":
        return None
    ident = term["term_id"]
    has_id = ident in [local.get("id"), local.get("go_id"), *local.get("standard_ids", [])]
    record = local.get("definition_evidence")
    source = term.get("source")
    if (has_id and source and source == term_source(ident) and _source_record(record, source, ident)
            and norm(record.get("definition") or "") == norm(definition)
            and norm(record["name"]) == norm(local["name"]) and not taxon_conflict(record, term)):
        return "bound_authority_record"
    if norm(definition) == norm(source_definition) and (
            standard_id_match or has_id
            or any(row.get("term_id") == ident and row.get("identity_evidence")
                   and row.get("match") != "verified_database_cross_reference" for row in local.get("reference_terms", []))):
        return "bound_identifier_and_matching_definition"
    return None


def deterministic_scope(local, term, *, standard_id_match=False):
    return bool(_scope_basis(local, term, standard_id_match=standard_id_match))


def base_type_parent(kind, record):
    name = norm(record.get("name") or "")
    if name in BASE_LABELS | {norm(kind)}:
        return True
    if kind != "Protein" or not _taxa(record):
        return False
    definition = norm(record.get("definition") or "").rstrip(".")
    taxon_only = re.fullmatch(
        r"(?:a |any )?protein (?:that (?:is )?)?(?:encoded in the genome of|found in|from) "
        r"(?:some |an? |the )?"
        r"([a-z]+(?: [a-z]+){0,2})", definition,
    )
    labels = {name, *(norm(label) for label in (record.get("synonyms") or []))}
    return bool(taxon_only and f"{taxon_only[1]} protein" in labels)


def adjudicate_direct(review, *, direct, scoped=False, generic=False):
    if review.get("incomplete_review"):
        return review
    updates = {"informative": False} if generic else {}
    if direct and scoped:
        updates.update(same_endpoint_scope=True, informative=not generic)
    if direct and (scoped or review["same_endpoint_scope"]):
        updates["relation_type"] = "IS_A"
    if all(review[key] == value for key, value in updates.items()):
        return review
    reason = ("The cited authority class adds only base-type or taxon scope." if generic
              else "The scope-matched authority record asserts direct IS_A.")
    return {**review, **updates, "model_review": review, "reason": reason}


def verified_relation(review):
    return review["relation_type"] == "IS_A" and review["same_endpoint_scope"]


def accepted_relation(review):
    return verified_relation(review) and review["informative"]


def _reachable(graph, child):
    ancestors = set()
    queue = deque(graph.get(child, ()))
    while queue:
        parent = queue.popleft()
        if parent not in ancestors:
            ancestors.add(parent)
            queue.extend(graph.get(parent, ()))
    return ancestors


def _term_key(term):
    return term["source"], term["term_id"]


def _parent_key(row):
    local = row["local_parent"]
    return (None, local["id"]) if local else _term_key(row["term"])


def _choose(dag, accepted, prior_candidates):
    """Keep minimal, incomparable parents under the available directed subclass evidence."""
    source_graph, local_graph = defaultdict(set), defaultdict(set)
    for child, parent in dag.edges:
        local_graph[child].add(parent)
    for row in prior_candidates:
        term = row["term"]
        for edge in _relations(term):
            parent = {"source": edge["parent_source"], "term_id": edge["parent_id"]}
            if direct_source_is_a(term, parent, [edge]):
                source_graph[_term_key(term)].add(_term_key(parent))

    unique = {}
    for row in sorted(accepted, key=lambda item: item["id"]):
        key = _parent_key(row)
        if key in unique:
            row["preference"] = {"status": "duplicate_parent", "retained_id": unique[key]["id"]}
        else:
            unique[key] = row
    options = list(unique.values())
    by_name = defaultdict(list)
    for row in options:
        if not row["local_parent"]:
            by_name[norm(row["term"]["name"])].append(row)
    for matches in by_name.values():
        if len(matches) > 1:
            for row in matches:
                row["preference"] = {"status": "same_name_distinct_authority_ids"}

    dominance = {}
    for row in options:
        if row.get("preference"):
            continue
        ancestors = _reachable(source_graph, _term_key(row["term"]))
        local = row["local_parent"]
        local_ancestors = _reachable(local_graph, local["id"]) if local else set()
        dominance[row["id"]] = {
            candidate["id"] for candidate in options if not candidate.get("preference")
            and (_term_key(candidate["term"]) in ancestors
                 or (candidate["local_parent"] or {}).get("id") in local_ancestors)
        }
    contradictory = {
        child for child, broader in dominance.items()
        if any(child in dominance.get(parent, ()) for parent in broader)
    }
    selected = []
    for row in options:
        if row.get("preference"):
            continue
        ident = row["id"]
        if ident in contradictory:
            row["preference"] = {"status": "contradictory_parent_ancestry"}
        elif narrower := sorted(child for child, broader in dominance.items()
                                 if child not in contradictory and ident in broader):
            row["preference"] = {"status": "broader_redundant", "narrower_ids": narrower}
        else:
            row["preference"] = {"status": "specific_supported_parent"}
            selected.append(row)
    return selected


def find_parent(dag, concept, *, request, lookup, budget, depth, groundings, reference_audits):
    """Return the most specific supported parents and the bounded search and review audit."""
    seeds = _seed_terms(concept, reference_audits)
    audit = {
        "status": "no_verified_parent", "lookup_budget": budget, "depth_limit": depth,
        "lookup_attempts": 0, "budget_exhausted": False, "depth_limited": False,
        "seed_terms": seeds, "lookups": [], "ignored_relations": [], "candidates": [], "review_rounds": [],
        "auxiliary_evidence": concept.get("auxiliary_evidence", []),
    }
    if not seeds:
        return [], {**audit, "status": "no_verified_child_identity"}
    for paths in _walk(seeds, lookup, budget, depth, audit):
        rows = _candidate_rows(dag, concept, paths, groundings)
        audit["candidates"].extend(rows)
        eligible = [row for row in rows if row["status"] == "candidate"]
        if not eligible:
            continue
        review_trace = {"depth": paths[0]["depth"]}
        audit["review_rounds"].append(review_trace)
        reviewed = review_relations(
            request, "enrichment_parent_fallback_review", eligible,
            {"target": describe(concept), "definition_evidence": concept.get("definition_evidence"),
             "auxiliary_evidence": audit["auxiliary_evidence"]},
            trace=review_trace,
        )
        accepted = []
        for row in eligible:
            direct = direct_source_is_a(row["start"], row["term"], row["path"])
            local = row["local_parent"]
            child_basis = _scope_basis(concept, row["start"])
            parent_basis = (_scope_basis(dag.nodes[local["id"]], row["term"],
                            standard_id_match=row["local_match"] == "standard_id")
                            if local else "imported_authority_record")
            scoped = bool(child_basis and parent_basis)
            generic = base_type_parent(concept["kind"], row["term"])
            review = adjudicate_direct(reviewed[row["id"]], direct=direct, scoped=scoped, generic=generic)
            row["review"] = review
            row["direct_source_is_a"] = direct
            row["deterministic_scope"] = scoped
            row["identity_scope_basis"] = {"child": child_basis, "parent": parent_basis}
            if verified_relation(review):
                row["status"] = "verified"
                if review["informative"]:
                    accepted.append(row)
                else:
                    row["preference"] = {"status": "not_informative"}
            else:
                row["status"] = "unreviewed" if review.get("incomplete_review") else "not_verified"
        if not review_trace["complete"]:
            return [], {**audit, "status": "incomplete_candidate_review"}
        if accepted:
            selected = _choose(dag, accepted, audit["candidates"])
            if not selected:
                return [], {**audit, "status": "ambiguous_verified_parents"}
            ids = [row["id"] for row in selected]
            return selected, {**audit, "status": "verified", "selected_parent_ids": ids,
                              "selected_parent_id": ids[0] if len(ids) == 1 else None}
    return [], audit
