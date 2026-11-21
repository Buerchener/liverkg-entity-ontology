import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from entity_ontology.hierarchy.go_reference.ols import EXACT_SYNONYM, IAO_DEFINITION, OBO, OBO_IN_OWL, OLSError
from entity_ontology.hierarchy.ontology_ols import HGNC_REPORT, OntologyOLS


def concept(ident, name, kind="Disease", aliases=()):
    return {"id": ident, "kind": kind, "name": name, "aliases": list(aliases)}


def term(term_id, ontology, label, **fields):
    iri = OBO + term_id.replace(":", "_")
    return {
        "curie": term_id, "iri": iri, "ontologyId": ontology, "type": ["class"],
        "label": [label], "isObsolete": False, "isDefiningOntology": True,
        IAO_DEFINITION: {"type": ["reification"], "value": f"Definition of {label}."},
        **fields,
    }


def hgnc_gene(number, symbol, name, **fields):
    return {
        "curie": f"hgnc:{number}", "iri": HGNC_REPORT + number, "ontologyId": "hgnc",
        "type": ["class"], "label": [symbol], "definition": [name],
        "isObsolete": False, "isDefiningOntology": True, **fields,
    }


def hgnc_alias(text):
    return {"type": ["reification"], "value": text,
            "axioms": [{OBO_IN_OWL + "hasSynonymType": OBO + "OMO_0003016"}]}


class FakeOLS:
    def __init__(self, queries, *rows, name_queries=None):
        self.queries = queries
        self.rows = {row["iri"]: row for row in rows}
        self.lookups = []
        self.name_queries = name_queries or {}

    def __call__(self, url):
        parts = urlsplit(url)
        self.lookups.append(parts)
        if parts.path.endswith("/search"):
            params = parse_qs(parts.query)
            docs = []
            for row in self.queries.get(params["q"][0], []):
                docs.append({
                    "iri": row["iri"], "obo_id": row["curie"], "ontology_name": row["ontologyId"],
                    "label": row["label"][0], "type": row["type"][0],
                })
            return {"response": {"docs": docs, "numFound": len(docs)}}
        if parts.path.endswith("/classes"):
            query = parse_qs(parts.query)["search"][0]
            rows = self.name_queries.get(query, [])
            return {"elements": rows, "totalElements": len(rows)}
        iri = unquote(unquote(parts.path.rsplit("/", 1)[-1]))
        return self.rows[iri]


class OntologyOLSTests(unittest.TestCase):
    def test_ols_hgnc_is_identity_only_even_when_external_gene_classes_have_edges(self):
        first = hgnc_gene("11998", "TP53", "tumor protein p53", synonym=[hgnc_alias("p53")])
        second = hgnc_gene("11999", "TP53BP1", "tumor protein p53 binding protein 1",
                           directParent=[first["iri"]])
        remote = FakeOLS({"TP53": [first], "p53": [first], "TP53BP1": [second]}, first, second)
        target = concept("A", "human TP53 gene", "Gene")
        same = concept("B", "p53", "Gene")
        other = concept("C", "TP53BP1", "Gene")
        result = OntologyOLS(get_json=remote).lookup(target, [
            {"child": target, "parent": same}, {"child": other, "parent": target},
        ])

        self.assertEqual(result["source"], "HGNC")
        self.assertEqual(result["status"], "REFERENCE_ONLY")
        self.assertEqual(result["relations"], [])
        self.assertEqual([row["term_id"] for row in result["terms"]],
                         ["HGNC:11998", "HGNC:11998", "HGNC:11999"])
        self.assertTrue(all(url.hostname == "www.ebi.ac.uk" for url in remote.lookups))

    def test_gene_coverage_uses_exact_official_name_and_typed_aliases(self):
        name = "tumor protein p53"
        related = "Li-Fraumeni syndrome"
        first = hgnc_gene("11998", "TP53", name, synonym=[related, hgnc_alias("p53")])
        other = hgnc_gene("11999", "TP53BP1", f"{name} binding protein 1", synonym=[hgnc_alias("p53")])
        obsolete = hgnc_gene("12000", "TEST", "withdrawn record", isObsolete=True)
        remote = FakeOLS({related: [first], "p53": [first, other], "TEST": [obsolete]},
                         first, other, obsolete, name_queries={name: [other, first]})
        ols = OntologyOLS(get_json=remote)

        self.assertEqual(ols.lookup(concept("A", name, "Gene"), [])["terms"][0]["term_id"], "HGNC:11998")
        self.assertEqual(ols.lookup(concept("B", "p53", "Gene"), [])["status"], "UNCERTAIN")
        self.assertEqual(ols.lookup(concept("C", related, "Gene"), [])["status"], "NO_MATCH")
        self.assertEqual(ols.lookup(concept("D", "TEST", "Gene"), [])["status"], "NO_MATCH")

    def test_gene_skips_known_nonhuman_and_rejects_incomplete_search(self):
        calls = []

        def partial(url):
            calls.append(url)
            return {"response": {"docs": [], "numFound": 7}}

        ols = OntologyOLS(get_json=partial)
        other_species = [concept("A", "mouse Tp53", "Gene"),
                         {**concept("B", "Tp53", "Gene"), "taxon_id": 10090}]
        for target in other_species:
            self.assertEqual(ols.lookup(target, [])["status"], "UNCERTAIN")
        self.assertEqual(calls, [])
        self.assertEqual(ols.lookup(concept("C", "TP53", "Gene"), [])["status"], "LOOKUP_FAILED")

    def test_only_active_and_native_ontology_class_details_are_accepted(self):
        native = term("MONDO:0000001", "mondo", "example concept")
        obsolete = term("MONDO:0000002", "mondo", "example concept", isObsolete=True)
        imported = term("MONDO:0000003", "mondo", "example concept", isDefiningOntology=False)
        wrong_prefix = term("CL:0000001", "mondo", "example concept")
        remote = FakeOLS({"example concept": [native, obsolete, imported, wrong_prefix]},
                         native, obsolete, imported, wrong_prefix)
        outcome = OntologyOLS(get_json=remote).lookup(concept("A", "example concept"), [])

        self.assertEqual(outcome["status"], "REFERENCE_ONLY")
        self.assertEqual(outcome["source"], "MONDO")
        self.assertEqual([row["term_id"] for row in outcome["terms"]], ["MONDO:0000001"])
        self.assertEqual(outcome["terms"][0]["definition"], "Definition of example concept.")

    def test_external_edges_use_direct_subclass_and_verified_endpoints(self):
        parent = term("MONDO:0000001", "mondo", "test disease")
        related = term("MONDO:0000002", "mondo", "related disease")
        child = term(
            "MONDO:0000003", "mondo", "test disease subtype",
            directParent=[{"type": ["reification"], "value": parent["iri"]}],
            directAncestor=[parent["iri"], related["iri"]],
        )
        remote = FakeOLS({"test disease": [parent], "test disease subtype": [child],
                          "related disease": [related]}, parent, related, child)
        focus = concept("C", "test disease subtype")
        reversed_pair = {"id": "p1", "child": concept("P", "test disease"), "parent": focus}
        related_pair = {"id": "p2", "child": focus, "parent": concept("R", "related disease")}
        result = OntologyOLS(get_json=remote).lookup(focus, [reversed_pair, related_pair])

        self.assertEqual(len(result["terms"]), 3)
        self.assertEqual(len(result["relations"]), 1)
        edge = result["relations"][0]
        self.assertEqual((edge["child_id"], edge["parent_id"], edge["predicate"]),
                         ("MONDO:0000003", "MONDO:0000001", "is_a"))
        self.assertEqual(edge["source_url"], result["terms"][0]["source_url"])

    def test_synonyms_only_count_when_detail_marks_them_exact(self):
        exact = term("CHEBI:1", "chebi", "chemical", **{EXACT_SYNONYM: ["metabolite"]})
        unrelated = term(
            "CHEBI:2", "chebi", "not the chemical",
            **{"http://www.geneontology.org/formats/oboInOwl#hasRelatedSynonym": ["metabolite"]},
        )
        remote = FakeOLS({"metabolite": [exact, unrelated]}, exact, unrelated)
        output = OntologyOLS(get_json=remote).lookup(concept("M", "metabolite", "Metabolite"), [])

        self.assertEqual([(r["term_id"], r["match"]) for r in output["terms"]],
                         [("CHEBI:1", "exact_synonym")])

    def test_pro_uses_native_pr_namespace_even_without_ols_defining_flag(self):
        pr = term("PR:P02452", "pr", "collagen alpha-1(I) chain (human)",
                  curie="PR_P02452", isDefiningOntology=False, appearsIn=["pr"])
        imported = term("CL:0000019", "pr", "collagen alpha-1(I) chain (human)")
        name = pr["label"][0]
        remote = FakeOLS({name: [imported, pr]}, pr, imported)
        result = OntologyOLS(get_json=remote).lookup(concept("P", name, "Protein"), [])

        self.assertEqual([row["term_id"] for row in result["terms"]], ["PR:P02452"])
        self.assertNotIn("local", parse_qs(remote.lookups[0].query))

    def test_empty_and_failed_lookups_have_distinct_statuses(self):
        focus = concept("A", "rare disease")
        self.assertEqual(OntologyOLS(get_json=FakeOLS({})).lookup(focus, [])["status"], "NO_MATCH")

        def unavailable(_):
            raise OLSError("OLS unavailable")

        self.assertEqual(OntologyOLS(get_json=unavailable).lookup(focus, [])["status"], "LOOKUP_FAILED")


if __name__ == "__main__":
    unittest.main()
