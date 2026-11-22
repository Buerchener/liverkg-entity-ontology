"""A functional annotation or a name-only crosswalk cannot establish local IS_A."""

from unittest import TestCase

from entity_ontology.enrichment.parent_fallback import _seed_terms
from entity_ontology.enrichment.placement import _typed_validation, place_verified
from entity_ontology.hierarchy.entity_placement_dag import EntityDAG
from entity_ontology.hierarchy.placement import review_item
from entity_ontology.tests.test_enrichment_parent_fallback import lookup_for, model, node, term


class EnrichmentAuxiliaryVetoTest(TestCase):
    def test_exact_containment_vetoes_local_model_edge_and_unbound_name_stays_uncertain(self):
        child = {**node("child", "Branch", kind="Pathway"), "standard_ids": ["Reactome:R-HSA-100"]}
        child["auxiliary_evidence"] = [{"subject_id": "Reactome:R-HSA-100", "object_id": "Reactome:R-HSA-200",
                                       "relation": "PART_OF", "predicate": "inverse(hasEvent)",
                                       "source_url": "https://example.test/ancestors"}]
        for ids, expected in [(["Reactome:R-HSA-200"], "PART_OF"), ([], "UNCERTAIN")]:
            dag = EntityDAG(["Pathway"])
            dag.place({**node("parent", "Larger event", kind="Pathway"), "standard_ids": ids})
            staged, audit = place_verified(dag, child, typed_review=True,
                                          request=model(suggestion=False, comparisons={"parent": (True, False)}))
            self.assertIs(staged, dag)
            row, = audit["typed_relation_reviews"]
            self.assertEqual(row["review"]["relation_type"], expected)
            self.assertEqual(row["review"]["model_review"]["relation_type"], "IS_A")

    def test_go_name_and_annotation_are_not_evidence_of_pathway_identity(self):
        go = {**term("GO:0000001", "Branch"), "source": "GO", "concept_id": "child", "match": "exact_label"}
        local = {**node("child", "Branch", kind="Pathway"), "standard_ids": ["Reactome:R-HSA-100", "GO:0000001"]}
        link = {"subject_id": "Reactome:R-HSA-100", "object_id": go["term_id"],
                "predicate": "goBiologicalProcess", "source_url": "https://example.test/pathway"}
        for predicate, expected in [("goBiologicalProcess", False), ("skos:exactMatch", True)]:
            bound = {**go, "identity_evidence": {**link, "predicate": predicate}}
            endpoint = {**local, "reference_terms": [bound]}
            self.assertEqual(bool(_seed_terms(endpoint, [{"result": {"terms": [go]}}])), expected)
            goal = {**node("parent", "Wider process", kind="Pathway"), "standard_ids": ["GO:0000002"]}
            dag = EntityDAG(["Pathway"])
            dag.place(goal)
            dag.place(endpoint, parent_ids=["parent"])
            pair = review_item("child->parent", endpoint, goal)
            edge = {"child_id": go["term_id"], "parent_id": "GO:0000002", "predicate": "is_a",
                    "source": "GO", "source_url": go["source_url"]}
            proof = {"child_term": go, "parent_term": {
                         **term("GO:0000002", "Wider process"), "concept_id": "parent", "source": "GO"},
                     "is_a_evidence": [edge]}
            audit = {"reference_audits": [], "edge_evidence": [{"child_id": "child", "parent_id": "parent",
                                                                 "reference": proof}]}
            rejected = _typed_validation(dag, endpoint, [pair], model(), audit)
            self.assertEqual(not rejected, expected)

    def test_pro_direct_parent_still_works_alongside_component_annotation(self):
        anchor = {**term("PR:123", "Target protein", parents=["PR:456"]), "source": "PRO"}
        target = node("child", "Target protein", kind="Protein", references=[anchor])
        target["auxiliary_evidence"] = [{"subject_id": "UniProt:P12345", "relation": "RELATED",
                                        "predicate": "SUBUNIT", "source_url": "https://example.test/subunit"}]
        dag = EntityDAG(["Protein"])
        actual = {**term("PR:456", "Parent protein class"), "source": "PRO"}
        staged, audit = place_verified(dag, target, request=model(), parent_lookup=lookup_for(actual))
        self.assertEqual(audit["status"], "inserted")
        self.assertEqual(staged.nodes[audit["generated_parent_id"]]["standard_ids"], ["PR:456"])
        self.assertEqual(audit["typed_relation_reviews"][0]["review"]["relation_type"], "UNCERTAIN")
