"""Retrieval guards: candidates must not become accidental identity decisions."""

import unittest

from entity_ontology.alignment.candidates import CandidateRetriever, canonical_standard_id


def node(ident, name, kind="Disease", **extra):
    return {"id": ident, "name": name, "kind": kind, "definition": "", "aliases": [],
            "standard_ids": [], "parent_ids": [], "is_root": False, **extra}


class FakeEmbeddings:
    def __init__(self):
        self.calls = []

    def for_texts(self, texts):
        self.calls.append(dict(texts))
        return [[1.0, 0.0] if "broad disorder" in text or key.startswith("kg:") else [0.0, 1.0]
                for key, text in texts.items()]


class CandidateRetrievalTests(unittest.TestCase):
    def test_names_are_type_filtered_and_do_not_conflate_gene_product(self):
        retriever = CandidateRetriever([
            node("gene", "FAS", "Gene"), node("protein", "FAS", "Protein")])
        rows = retriever.retrieve(node("kg", "FAS", "Gene"))
        self.assertEqual([row["ontology_node_id"] for row in rows], ["gene"])
        self.assertEqual(rows[0]["retrieval"][0]["method"], "exact_name_or_alias")
        self.assertNotIn("relation_type", rows[0])

    def test_identifier_namespaces_and_padding_do_not_collapse(self):
        retriever = CandidateRetriever([
            node("one", "unrelated A", "Gene", standard_ids=["NCBIGene:355"]),
            node("two", "unrelated B", "Gene", standard_ids=["HGNC:355"]),
            node("three", "unrelated C", "Gene", standard_ids=["NCBIGene:0355"]),
        ])
        rows = retriever.retrieve(node("kg", "different name", "Gene", standard_ids=["EntrezGene:355"]))
        self.assertEqual([row["ontology_node_id"] for row in rows], ["one"])
        self.assertEqual(rows[0]["retrieval"][0]["shared_ids"], ["NCBIGene:355"])
        for value in ["355", 355, "id:355", "GO:１２３", "NCBIGene: 355", "HGNC:-355"]:
            self.assertIsNone(canonical_standard_id(value), value)
        self.assertEqual(canonical_standard_id("go_0008150"), "GO:0008150")

    def test_nonleaf_eligible_and_more_specific_tie_first(self):
        retriever = CandidateRetriever([
            node("root", "Disease", is_root=True),
            node("broad", "disorder", parent_ids=["root"]),
            node("mid", "disorder", parent_ids=["broad"]),
            node("leaf", "specific disease", parent_ids=["mid"]),
        ])
        rows = retriever.retrieve(node("kg", "disorder"))
        self.assertEqual([row["ontology_node_id"] for row in rows], ["mid", "broad"])
        self.assertEqual(rows[0]["specificity_depth"], 2)
        self.assertTrue(retriever.retrieval_summary("kg")["ambiguous_exact"])

    def test_exact_collision_truncation_is_explicit(self):
        retriever = CandidateRetriever([node(str(i), "same") for i in range(5)], max_candidates=2)
        self.assertEqual(len(retriever.retrieve(node("kg", "same"))), 2)
        self.assertEqual(retriever.retrieval_summary("kg"), {
            "total_candidates": 5, "returned_candidates": 2, "truncated": True,
            "exact_candidate_count": 5, "ambiguous_exact": True, "entity_type": "Disease",
            "type_supported": True, "embedding_enabled": False, "policy": "retrieval_only_not_alignment",
        })

    def test_alias_matches_use_existing_unicode_normalization(self):
        retriever = CandidateRetriever([node("ont", "Long disorder name", aliases=["ＮＡＦＬＤ"])])
        rows = retriever.retrieve(node("kg", "  nafld  "))
        self.assertEqual(rows[0]["retrieval"][0]["shared_names"], ["nafld"])

    def test_unknown_type_root_and_unrelated_node_have_no_candidate(self):
        retriever = CandidateRetriever([node("root", "Disease", is_root=True), node("one", "fibrosis")])
        for item in [node("unknown", "fibrosis", "Other"), node("rootkg", "Disease"), node("none", "hypertension")]:
            self.assertEqual(retriever.retrieve(item), [])

    def test_lexical_fallback_can_find_abstract_nonleaf(self):
        retriever = CandidateRetriever([
            node("parent", "fatty liver disease", definition="A disorder of hepatic fat accumulation"),
            node("child", "another condition", parent_ids=["parent"]),
            node("other", "inflammation"),
        ])
        rows = retriever.retrieve(node("kg", "nonalcoholic fatty liver disease"))
        self.assertEqual(rows[0]["ontology_node_id"], "parent")
        self.assertEqual(rows[0]["retrieval"][0]["method"], "lexical")

    def test_embeddings_reuse_type_index_and_are_only_fallback(self):
        embeddings = FakeEmbeddings()
        retriever = CandidateRetriever([
            node("a", "broad disorder"), node("b", "different condition"), node("gene", "locus", "Gene"),
        ], embeddings=embeddings)
        self.assertEqual(retriever.retrieve(node("exact", "broad disorder"))[0]["ontology_node_id"], "a")
        self.assertEqual(embeddings.calls, [])
        rows = retriever.retrieve(node("kg1", "unfamiliar"))
        self.assertEqual(rows[0]["ontology_node_id"], "a")
        self.assertEqual(rows[0]["retrieval"][0], {"method": "embedding", "score": 1.0})
        retriever.retrieve(node("kg2", "another unfamiliar"))
        self.assertEqual(len(embeddings.calls), 3)
        self.assertEqual(set(embeddings.calls[0]), {"ontology:a", "ontology:b"})

    def test_cycles_and_duplicate_ids_fail_instead_of_looping(self):
        with self.assertRaisesRegex(ValueError, "cycle"):
            CandidateRetriever([node("a", "a", parent_ids=["b"]), node("b", "b", parent_ids=["a"])])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            CandidateRetriever([node("a", "one"), node("a", "two")])


if __name__ == "__main__":
    unittest.main()
