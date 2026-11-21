import unittest
from urllib.parse import urlsplit

from entity_ontology.hierarchy.hgnc_reference import HGNCReference


TP53 = {
    "hgnc_id": "HGNC:11998", "symbol": "TP53", "name": "tumor protein p53",
    "status": "Approved", "locus_type": "gene with protein product",
    "gene_group": ["Tumor protein p53 family"], "alias_symbol": ["P53"],
}


def gene(name, **fields):
    return {"id": name, "kind": "Gene", "name": name, "definition": "", "aliases": [], **fields}


def endpoint(pages):
    def get(url):
        rows = pages.get(urlsplit(url).path, [])
        return {"response": {"docs": rows, "numFound": len(rows)}}
    return get


class HGNCReferenceTest(unittest.TestCase):
    def test_use_identity_without_parent_relation(self):
        lookup = HGNCReference(get_json=endpoint({"/fetch/symbol/TP53": [TP53]}))
        result = lookup.lookup(gene("TP53 gene"), [{
            "id": "pair", "child": gene("TP53 gene"), "parent": gene("tumor protein p53 family"),
        }])

        self.assertEqual(result["status"], "REFERENCE_ONLY")
        self.assertEqual(result["terms"][0]["term_id"], "HGNC:11998")
        self.assertEqual(result["terms"][0]["concept_id"], "TP53 gene")
        self.assertIsNone(result["terms"][0]["definition"])
        self.assertEqual(result["relations"], [])

    def test_reject_ambiguous_alias_and_ignore_fuzzy_or_withdrawn_rows(self):
        other = dict(TP53, hgnc_id="HGNC:1000", symbol="ABC")
        lookup = HGNCReference(get_json=endpoint({"/fetch/alias_symbol/P53": [TP53, other]}))
        result = lookup.lookup(gene("P53"), [])
        self.assertEqual(result["status"], "UNCERTAIN")
        self.assertEqual(result["terms"], [])

        withdrawn = dict(TP53, symbol="TP5", status="Entry Withdrawn")
        lookup = HGNCReference(get_json=endpoint({"/fetch/symbol/TP5": [TP53, withdrawn]}))
        self.assertEqual(lookup.lookup(gene("TP5"), [])["status"], "NO_MATCH")

    def test_skip_explicit_nonhuman_and_mark_incomplete_fetch_failed(self):
        requests = []

        def partial(url):
            requests.append(url)
            return {"response": {"docs": [TP53], "numFound": 2}}

        lookup = HGNCReference(get_json=partial)
        for concept in [gene("mouse Tp53"), gene("Tp53", taxon_id=10090)]:
            self.assertEqual(lookup.lookup(concept, [])["status"], "UNCERTAIN")
        self.assertEqual(requests, [])

        result = lookup.lookup(gene("TP53"), [])
        self.assertEqual(result["status"], "LOOKUP_FAILED")
        self.assertEqual(result["terms"], [])
        self.assertTrue(requests)

    def test_cap_network_calls_across_question_endpoints(self):
        lookup = HGNCReference(get_json=endpoint({}))
        target = gene("x0")
        questions = [{"child": target, "parent": gene(f"x{i}")} for i in range(1, 12)]
        result = lookup.lookup(target, questions)
        self.assertEqual(result["status"], "UNCERTAIN")
        self.assertEqual(len(result["requests"]), 8)
        self.assertIn("budget_exhausted", result["issues"])
