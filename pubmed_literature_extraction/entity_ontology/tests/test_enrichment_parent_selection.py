"""Verify authority evidence, preference and graph publication independently."""

from unittest import TestCase

from entity_ontology.enrichment.placement import place_verified
from entity_ontology.hierarchy.entity_placement_dag import EntityDAG
from entity_ontology.tests.test_enrichment_parent_fallback import lookup_for, model, node, term


def target_for(*parent_ids, kind="CellType"):
    source = term("T:0", "Target type", parents=parent_ids)
    return {**node("target", "Target type", kind=kind, references=[source]),
            "definition": source["definition"], "standard_ids": [source["term_id"]]}


def fallback_request(original=None, decide=None):
    original = original or model()

    def request(task):
        if task[0] != "enrichment_parent_fallback_review":
            return original(task)
        rows = original(task)
        return decide(task[2]["items"], rows)

    return request


class ParentSelectionTest(TestCase):
    def test_bound_scope_keeps_two_direct_parents_despite_model_preference(self):
        dag = EntityDAG(["CellType"])
        target = target_for("T:1", "T:2")
        records = [term("T:1", "Subtype by lineage"), term("T:2", "Subtype by compartment")]
        request = fallback_request(decide=lambda _, rows: [
            {**row, "relation_type": "UNCERTAIN", "same_endpoint_scope": False, "informative": False}
            for row in rows
        ])
        staged, audit = place_verified(dag, target, request=request, parent_lookup=lookup_for(*records))
        self.assertEqual(audit["status"], "inserted")
        self.assertEqual(set(audit["generated_parent_ids"]), set(audit["parent_ids"]))
        self.assertEqual(len(audit["parent_ids"]), 2)
        self.assertEqual({staged.nodes[i]["name"] for i in audit["parent_ids"]}, {r["name"] for r in records})
        self.assertEqual(len(audit["edge_evidence"]), 4)
        reviews = [row["review"] for row in audit["parent_fallback"]["candidates"]]
        self.assertEqual([row["relation_type"] for row in reviews], ["IS_A", "IS_A"])
        self.assertTrue(all(row["model_review"]["relation_type"] == "UNCERTAIN" for row in reviews))

    def test_no_label_only_local_scope_override(self):
        dag = EntityDAG(["CellType"])
        dag.place(node("local", "Homonymous class"))
        target = target_for("T:1")
        request = fallback_request(decide=lambda _, rows: [
            {**row, "relation_type": "UNCERTAIN", "same_endpoint_scope": False} for row in rows
        ])
        staged, audit = place_verified(
            dag, target, request=request, parent_lookup=lookup_for(term("T:1", "Homonymous class")),
        )
        self.assertIs(staged, dag)
        row, = audit["parent_fallback"]["candidates"]
        self.assertEqual(row["local_match"], "exact_label_or_synonym")
        self.assertFalse(row["deterministic_scope"])
        self.assertEqual(row["status"], "not_verified")

    def test_exact_id_and_definition_can_reuse_local_parent_without_scope_override_by_model(self):
        dag = EntityDAG(["CellType"])
        authority = term("T:1", "Local class")
        local = {**node("local", "Local class"), "standard_ids": [authority["term_id"]],
                 "definition": authority["definition"]}
        dag.place(local)
        request = fallback_request(decide=lambda _, rows: [
            {**row, "relation_type": "UNCERTAIN", "same_endpoint_scope": False, "informative": False}
            for row in rows
        ])
        _, audit = place_verified(
            dag, target_for("T:1"), request=request, parent_lookup=lookup_for(authority),
        )
        self.assertEqual(audit["parent_ids"], ["local"])
        self.assertEqual(audit["generated_parent_ids"], [])

    def test_taxon_only_protein_is_verified_relation_but_not_selected(self):
        dag = EntityDAG(["Protein"])
        target = target_for("T:1", kind="Protein")
        broad = {**term("T:1", "Example species protein",
                        definition="A protein that is encoded in the genome of Example species."),
                 "taxon_ids": ["123"]}
        staged, audit = place_verified(dag, target, request=model(), parent_lookup=lookup_for(broad))
        self.assertIs(staged, dag)
        row, = audit["parent_fallback"]["candidates"]
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["preference"]["status"], "not_informative")

    def test_incompatible_taxon_cannot_be_overridden_by_model(self):
        dag = EntityDAG(["CellType"])
        authority = {**term("T:1", "Scoped class"), "taxon_ids": ["123"]}
        local = {**node("local", "Scoped class"), "standard_ids": [authority["term_id"]],
                 "definition": authority["definition"], "taxon_id": "NCBITaxon:456"}
        dag.place(local)
        staged, audit = place_verified(
            dag, target_for("T:1"), request=model(), parent_lookup=lookup_for(authority),
        )
        self.assertIs(staged, dag)
        self.assertEqual(audit["parent_fallback"]["candidates"][0]["status"],
                         "authority_endpoint_taxon_conflict")

    def test_a_more_specific_parent_suppresses_authority_ancestor_without_negating_it(self):
        dag = EntityDAG(["CellType"])
        source = lookup_for(term("T:1", "Narrow parent", parents=["T:2"]), term("T:2", "Broader parent"))
        staged, audit = place_verified(dag, target_for("T:1", "T:2"), request=model(), parent_lookup=source)
        self.assertEqual([staged.nodes[x]["name"] for x in audit["parent_ids"]], ["Narrow parent"])
        broader = next(row for row in audit["parent_fallback"]["candidates"] if row["term"]["term_id"] == "T:2")
        self.assertEqual(broader["status"], "verified")
        self.assertEqual(broader["preference"]["status"], "broader_redundant")

    def test_discovery_runs_after_an_approved_parent_and_retains_incomparable_local_class(self):
        dag = EntityDAG(["CellType"])
        dag.place(node("local", "Independently supported prior parent"))
        target = target_for("T:1")
        request = model(suggestion=False, comparisons={"local": (True, False)})
        staged, audit = place_verified(
            dag, target, request=request, parent_lookup=lookup_for(term("T:1", "Another strict parent")),
        )
        self.assertEqual(audit["status"], "inserted")
        self.assertEqual(audit["parent_fallback"]["retained_original_parent_ids"], ["local"])
        self.assertEqual(len(audit["parent_ids"]), 2)
        self.assertIn(("target", "local"), staged.edges)
        self.assertIn("independent_definition_review", {edge["support"] for edge in audit["edge_evidence"]})

    def test_discovery_does_not_keep_an_original_parent_proven_redundant(self):
        dag = EntityDAG(["CellType"])
        broad = term("T:2", "Original broad parent")
        dag.place({**node("local", broad["name"]), "standard_ids": [broad["term_id"]]})
        request = model(suggestion=False, comparisons={"local": (True, False)})
        staged, audit = place_verified(
            dag, target_for("T:1", "T:2"), request=request,
            parent_lookup=lookup_for(term("T:1", "Specific class", parents=["T:2"]), broad),
        )
        self.assertEqual(audit["status"], "inserted")
        self.assertEqual(len(audit["parent_ids"]), 1)
        self.assertNotIn(("target", "local"), staged.edges)
        self.assertEqual(audit["parent_fallback"]["redundant_original_parent_ids"], ["local"])

    def test_partial_repair_targets_only_omitted_candidates(self):
        dag = EntityDAG(["CellType"])
        source = lookup_for(term("T:1", "First direct class"), term("T:2", "Second direct class"))
        seen = []

        def respond(items, rows):
            seen.append([row["id"] for row in items])
            return rows[:1]

        request = fallback_request(decide=respond)
        _, audit = place_verified(dag, target_for("T:1", "T:2"), request=request, parent_lookup=source)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1], seen[0][1:])
        self.assertTrue(audit["parent_fallback"]["review_rounds"][0]["complete"])
        self.assertEqual(len(audit["parent_ids"]), 2)

    def test_permanently_omitted_candidate_is_visible_and_never_committed(self):
        dag = EntityDAG(["CellType"])
        source = lookup_for(term("T:1", "First direct class"), term("T:2", "Second direct class"))
        calls = []

        def respond(items, rows):
            calls.append(items)
            return rows[:1] if len(calls) == 1 else []

        request = fallback_request(decide=respond)
        staged, audit = place_verified(dag, target_for("T:1", "T:2"), request=request, parent_lookup=source)
        self.assertIs(staged, dag)
        self.assertEqual(len(calls), 3)
        fallback = audit["parent_fallback"]
        self.assertEqual(fallback["status"], "incomplete_candidate_review")
        self.assertEqual(len(fallback["review_rounds"][0]["missing_ids"]), 1)
        self.assertEqual([c["status"] for c in fallback["candidates"]], ["verified", "unreviewed"])
