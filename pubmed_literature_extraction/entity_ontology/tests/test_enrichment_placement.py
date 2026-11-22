"""Verify placement gates and evidence without a provider."""

from unittest import TestCase
from unittest.mock import Mock

from entity_ontology.enrichment.placement import place_verified
from entity_ontology.hierarchy.entity_placement_dag import EntityDAG


def disease(ident, name):
    return {
        "id": ident, "name": name, "kind": "Disease", "definition": name,
        "aliases": [], "origin": "test_fixture", "source_mentions": [],
        "definition_evidence": [{"source": "fixture"}],
    }


def model(comparisons=None, *, generated=None, initial_review=True, independent_review=True):
    comparisons = comparisons or {}

    def call(task):
        stage, _, payload = task
        if stage == "incremental_propose":
            item, = payload["items"]
            rows = []
            for candidate in item["candidates"]:
                forward, reverse = comparisons.get(candidate["id"], (False, False))
                rows.append({"id": candidate["id"], "a_in_b": forward, "b_in_a": reverse,
                             "reason": "fixture proposal"})
            return [{"id": item["id"], "comparisons": rows, "new_parent": generated}]
        verdict = {"incremental_edge_review": initial_review,
                   "enrichment_hierarchy_review": independent_review}.get(stage, True)
        return [{"id": item["id"],
                 "supported": verdict.get(item["id"], True) if isinstance(verdict, dict) else verdict,
                 "reason": "fixture review"}
                for item in payload["items"]]

    return call


def ols_result(target, questions):
    pair, = questions
    parent_id = pair["parent"]["id"]
    return {
        "status": "REFERENCE_ONLY", "source": "MONDO", "sources_checked": ["MONDO"],
        "terms": [
            {"concept_id": target["id"], "term_id": "MONDO:1", "name": "Child",
             "definition": "External child definition", "source_url": "https://example.test/child"},
            {"concept_id": parent_id, "term_id": "MONDO:2", "name": "Official Parent",
             "definition": "External parent definition", "source_url": "https://example.test/parent"},
        ],
        "relations": [
            {"child_id": "MONDO:1", "parent_id": "MONDO:2", "predicate": "is_a",
             "source_url": "https://example.test/child"},
        ],
    }


class EnrichmentPlacementTest(TestCase):
    def setUp(self):
        self.dag = EntityDAG(["Disease"])
        self.target = disease("target", "Narrow disease")

    def test_type_root_requires_explicit_review(self):
        staged, result = place_verified(self.dag, self.target, request=model())
        self.assertEqual(result["status"], "inserted")
        self.assertEqual(result["edge_evidence"][0]["support"], "schema_type_assignment")
        self.assertIn(("target", "type:Disease"), staged.edges)
        self.assertEqual(self.dag.edges, set())

        staged, result = place_verified(
            self.dag, self.target, request=model(independent_review=False),
        )
        self.assertIs(staged, self.dag)
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("target", self.dag.nodes)

    def test_unresolved_parent_stops_root_fallback(self):
        self.dag.place(disease("neighbor", "Possible parent"))
        lookup = Mock(return_value={"status": "NO_MATCH", "source": "MONDO"})
        staged, result = place_verified(
            self.dag, self.target,
            request=model({"neighbor": (True, None)}, initial_review=None),
            entity_lookup=lookup,
        )
        lookup.assert_called_once()
        self.assertIs(staged, self.dag)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["unresolved_candidate_ids"], ["neighbor"])

    def test_multiple_parents_preserve_original_asserted_edges(self):
        self.dag.place(disease("parent1", "Disease superclass one"))
        self.dag.place(disease("parent2", "Disease superclass two"))
        self.dag.place(disease("old_child", "More specific disease"), parent_ids=["parent1"])
        before = set(self.dag.edges)
        staged, result = place_verified(self.dag, self.target, request=model({
            "parent1": (True, False), "parent2": (True, False), "old_child": (False, True),
        }))
        self.assertEqual(result["status"], "inserted")
        self.assertEqual(result["parent_ids"], ["parent1", "parent2"])
        self.assertEqual(result["child_ids"], ["old_child"])
        self.assertEqual(len(result["edge_evidence"]), 3)
        self.assertEqual(self.dag.edges, before)
        self.assertEqual(staged.edges - before,
                         {("target", "parent1"), ("target", "parent2"), ("old_child", "target")})
        self.assertTrue(before.issubset(staged.edges))
        self.assertEqual(result["proposal"]["removed_edges"],
                         [{"child_id": "old_child", "parent_id": "parent1"}])
        self.assertEqual(result["removed_edges"], [])

    def test_rejected_ancestor_vetoes_otherwise_supported_parent(self):
        self.dag.place(disease("ancestor", "Top disease superclass"))
        self.dag.place(disease("parent", "Narrower disease superclass"), parent_ids=["ancestor"])
        before = set(self.dag.edges)
        staged, result = place_verified(self.dag, self.target, request=model(
            {"parent": (True, False), "ancestor": (True, False)},
            initial_review={"existing:ancestor:parent": False},
        ))
        self.assertIs(staged, self.dag)
        self.assertEqual(staged.edges, before)
        self.assertEqual(result["status"], "needs_review")
        conflict, = result["transitive_conflicts"]
        self.assertEqual(conflict["isa_path"], ["target", "parent", "ancestor"])
        self.assertFalse(conflict["incompatible_review"]["supported"])

    def test_new_parent_needs_authoritative_definition_and_strict_edge(self):
        proposer = model(generated={"name": "Proposed Parent", "definition": "Model definition",
                                    "reason": "fixture proposal"})
        staged, result = place_verified(self.dag, self.target, request=proposer)
        self.assertIs(staged, self.dag)
        self.assertEqual(result["status"], "needs_review")

        staged, result = place_verified(self.dag, self.target, request=proposer, entity_lookup=ols_result)
        self.assertEqual(result["status"], "inserted")
        parent = staged.nodes[result["generated_parent_id"]]
        self.assertEqual(parent["name"], "Official Parent")
        self.assertEqual(parent["definition"], "External parent definition")
        self.assertEqual(parent["standard_ids"], ["MONDO:2"])
        self.assertEqual(parent["definition_evidence"]["term_id"], "MONDO:2")
        self.assertEqual({edge["support"] for edge in result["edge_evidence"]},
                         {"schema_type_assignment", "ols_informed_strict_review"})

    def test_generated_gene_parent_with_product_definition_stays_in_review(self):
        dag = EntityDAG(["Gene"])
        target = {**self.target, "kind": "Gene", "name": "fixture locus"}
        proposer = model(generated={"name": "Proposed gene class", "definition": "Proposed locus category.",
                                    "reason": "Fixture proposal."})

        def gene_reference(target, questions):
            q, = questions
            return {"status": "REFERENCE_ONLY", "source": "SO", "sources_checked": ["SO"], "terms": [
                {"concept_id": target["id"], "term_id": "SO:1", "name": target["name"],
                 "definition": "A particular gene locus.", "source_url": "https://example.test/locus"},
                {"concept_id": q["parent"]["id"], "term_id": "SO:2", "name": "RNA gene",
                 "definition": "A noncoding RNA involved in gene expression.",
                 "source_url": "https://example.test/category"}], "relations": [
                {"child_id": "SO:1", "parent_id": "SO:2", "predicate": "is_a",
                 "source_url": "https://example.test/locus"}]}

        staged, result = place_verified(dag, target, request=proposer, entity_lookup=gene_reference)
        self.assertIs(staged, dag)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("definition does not establish gene locus scope", result["reason"])

    def test_new_parent_collision_on_authoritative_alias_or_supplemental_id(self):
        proposer = model(generated={"name": "Proposed Parent", "definition": "Model definition",
                                    "reason": "fixture proposal"})
        for neighbor, known_ids in (
            ({**disease("existing", "Different name"), "aliases": ["Official Parent"]}, {}),
            (disease("existing", "Different name"), {"existing": ["MONDO:2"]}),
            (disease("MONDO:2", "Different name"), {}),
        ):
            with self.subTest(neighbor=neighbor, known_ids=known_ids):
                dag = EntityDAG(["Disease"])
                dag.place(neighbor)
                staged, result = place_verified(
                    dag, self.target, request=proposer, entity_lookup=ols_result, known_standard_ids=known_ids,
                )
                self.assertIs(staged, dag)
                self.assertEqual(result["status"], "needs_review")
                self.assertEqual(result["generated_parent_collision_id"], neighbor["id"])
