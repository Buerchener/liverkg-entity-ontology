"""Require cited whole-class Metabolite scope on import and subsequent reuse."""

from unittest import TestCase

from entity_ontology.enrichment.parent_fallback import metabolite_parent_has_biological_scope
from entity_ontology.enrichment.placement import load_dag, place_verified
from entity_ontology.hierarchy.entity_placement_dag import EntityDAG
from entity_ontology.tests.test_enrichment_parent_fallback import lookup_for, model, node, term


STRUCTURE_ONLY = "A class of ring compounds which contain an unsaturated skeleton."
BIOSYNTHETIC = "A class of ring compounds produced by cellular metabolism."


class MetaboliteScopeTest(TestCase):
    def test_definition_must_constrain_whole_class(self):
        definitions = {
            STRUCTURE_ONLY: False,
            BIOSYNTHETIC: True,
            "A class of secondary metabolites with a cyclic structure.": True,
            "Metabolites involved in synthesizing lipids.": True,
            "A compound formed during biological metabolism.": True,
            "A family of endogenously produced chemicals.": True,
            "A class of compounds biosynthesised by living cells.": True,
            "A compound which is a metabolite.": True,
            "A class of compounds derived formally from another molecule.": False,
            "A class of compounds which can be produced by bacteria.": False,
            "A class of compounds that may have a metabolite role.": False,
            "A group of aromatic compounds found in humans.": False,
            "A group of naturally occurring compounds.": False,
            "A compound not produced by cellular metabolism.": False,
            "A compound produced by metabolism or industrial synthesis.": False,
            "A ring compound. Some members are biosynthesized by plants.": False,
            "An inhibitor of compounds produced by cellular metabolism.": False,
        }
        for definition, wanted in definitions.items():
            with self.subTest(definition=definition):
                self.assertEqual(metabolite_parent_has_biological_scope(
                    {"source_url": "https://example.test/term", "definition": definition},
                ), wanted)
        self.assertFalse(metabolite_parent_has_biological_scope({"definition": BIOSYNTHETIC}))

    def test_fallback_can_inspect_beyond_an_ineligible_structural_class(self):
        dag = EntityDAG(["Metabolite"])
        child = term("T:0", "Test compound", parents=["T:1", "T:2"])
        target = node("new", "Test compound", kind="Metabolite", references=[child])
        structure = term("T:1", "Ring-only class", definition=STRUCTURE_ONLY)
        biological = term("T:2", "Ring products of metabolism", definition=BIOSYNTHETIC)

        staged, audit = place_verified(dag, target, request=model(), parent_lookup=lookup_for(structure, biological))
        self.assertEqual(audit["status"], "inserted")
        self.assertEqual(staged.nodes[audit["generated_parent_id"]]["definition"], BIOSYNTHETIC)
        self.assertEqual(staged.nodes[audit["generated_parent_id"]]["definition_evidence"], biological)
        candidate = audit["parent_fallback"]["candidates"][0]
        self.assertEqual(candidate["status"], "authority_definition_does_not_establish_metabolite_scope")

    def test_legacy_generated_parent_has_same_scope_gate(self):
        dag = EntityDAG(["Metabolite"])
        target = node("new", "Test compound", kind="Metabolite")

        def legacy_proof(child, questions):
            q, = questions
            source = "ChEBI"
            terms = [{"concept_id": child["id"], "term_id": "CHEBI:1", "name": target["name"]},
                     {"concept_id": q["parent"]["id"], "term_id": "CHEBI:2", "name": "Structural Parent",
                      "definition": STRUCTURE_ONLY, "source_url": "https://example.test/authority"}]
            edge = {"child_id": "CHEBI:1", "parent_id": "CHEBI:2", "predicate": "is_a"}
            return {"status": "REFERENCE_ONLY", "source": source, "terms": terms, "relations": [edge]}

        staged, audit = place_verified(dag, target, request=model(), entity_lookup=legacy_proof)
        self.assertIs(staged, dag)
        self.assertEqual(audit["status"], "needs_review")
        self.assertIn("whole-class metabolite scope", audit["reason"])

    def test_reloaded_reference_parent_requires_matching_authoritative_definition(self):
        for definition, authoritative, wanted in (
            (STRUCTURE_ONLY, STRUCTURE_ONLY, "needs_review"),
            (BIOSYNTHETIC, STRUCTURE_ONLY, "needs_review"),
            (BIOSYNTHETIC, BIOSYNTHETIC, "inserted"),
        ):
            with self.subTest(definition=definition, authoritative=authoritative):
                dag = EntityDAG(["Metabolite"])
                parent = {**node("existing", "Entity parent", kind="Metabolite"),
                          "origin": "kg_enrichment_reference_parent", "definition": definition,
                          "definition_evidence": {"source_url": "https://example.test/authority",
                                                  "definition": authoritative}}
                dag.place(parent)
                reloaded = load_dag(
                    {"roots": dag.roots, "nodes": list(dag.nodes.values()),
                     "edges": [{"child_id": c, "parent_id": p, "relation": "is_a"} for c, p in dag.edges]},
                )
                target = node("new", "Test compound", kind="Metabolite")
                request = model(comparisons={"existing": (True, False)})
                staged, audit = place_verified(reloaded, target, request=request, typed_review=True)
                self.assertEqual(audit["status"], wanted)
                if wanted == "needs_review":
                    self.assertIs(staged, reloaded)
                    self.assertIn("whole-class metabolite scope", audit["typed_relation_reviews"][0]["review"]["reason"])
