"""Check typed, bounded parent recovery without external services."""

from unittest import TestCase
from unittest.mock import Mock

from entity_ontology.enrichment.placement import place_verified
from entity_ontology.enrichment.parent_fallback import gene_definition_has_locus_scope
from entity_ontology.hierarchy.entity_placement_dag import EntityDAG


def term(ident, name, *, parents=(), definition="Sourced class definition.", synonyms=()):
    return {"term_id": ident, "name": name, "definition": definition, "source": "TestOntology",
            "source_url": f"https://example.test/{ident}", "direct_parent_ids": list(parents),
            "synonyms": list(synonyms)}


def lookup_for(*terms):
    records = {row["term_id"]: row for row in terms}
    return Mock(side_effect=lambda source, ident: {"record": records.get(ident)})


def node(ident, name, *, kind="CellType", references=(), aliases=()):
    return {"id": ident, "name": name, "definition": "Source-backed local scope.", "kind": kind,
            "aliases": list(aliases), "source_mentions": [], "origin": "test_fixture",
            "reference_terms": list(references), "definition_evidence": {"source": "fixture"}}


def model(*, suggestion=True, comparisons=None, original_reviews=None,
          typed=None, fallback_reviews=None, root_review=True):
    comparisons, original_reviews, typed = comparisons or {}, original_reviews or {}, typed or {}
    fallback_reviews = fallback_reviews or {}

    def request(task):
        stage, _, payload = task
        items = payload["items"]
        if stage == "incremental_propose":
            item, = items
            rows = [{"id": n["id"], "a_in_b": comparisons.get(n["id"], (False, False))[0],
                     "b_in_a": comparisons.get(n["id"], (False, False))[1],
                     "reason": "Fixture proposal."} for n in item["candidates"]]
            parent = {"name": "Unsupported initial class", "definition": "Hypothesis.", "reason": "Fixture."}
            return [{"id": item["id"], "comparisons": rows, "new_parent": parent if suggestion else None}]
        if stage in {"enrichment_parent_fallback_review", "enrichment_typed_relation_review"}:
            relations = fallback_reviews if stage == "enrichment_parent_fallback_review" else typed
            return [{"id": i["id"],
                     "relation_type": relations.get(i.get("term", {}).get("term_id", i["id"]), "IS_A"),
                     "same_endpoint_scope": True, "informative": True, "reason": "Fixture type."}
                    for i in items]
        if stage == "enrichment_parent_fallback_select":
            return [{"id": "selection", "candidate_id": None, "reason": "Equally specific."}]
        return [{"id": i["id"], "supported": root_review if stage == "enrichment_fallback_parent_type"
                 else original_reviews.get(i["id"], True), "reason": "Fixture scope."} for i in items]

    return request


class EnrichmentParentFallbackTest(TestCase):
    def setUp(self):
        self.dag = EntityDAG(["CellType"])

    def test_gene_import_requires_locus_scope_and_can_keep_a_strict_ancestor(self):
        locus = term("HGNC:31627", "MIR30C2", parents=["SO:0001265"])
        product = term("SO:0001265", "miRNA_gene", parents=["SO:0002342"],
                       definition="A small noncoding RNA of approximately 22 nucleotides in length "
                                  "which may be involved in regulation of gene expression.")
        parent = term("SO:0002342", "sncRNA_gene",
                      definition="A ncRNA_gene that encodes an ncRNA less than 200 nucleotides in length.")
        dag = EntityDAG(["Gene"])
        target = node("new", "MIR30C2", kind="Gene", references=[locus])
        staged, audit = place_verified(dag, target, request=model(), parent_lookup=lookup_for(product, parent))
        self.assertEqual(audit["status"], "inserted")
        self.assertEqual(staged.nodes[audit["generated_parent_id"]]["standard_ids"], ["SO:0002342"])
        self.assertEqual(audit["parent_fallback"]["candidates"][0]["status"],
                         "authority_definition_does_not_establish_gene_scope")
        self.assertEqual([e["parent_id"] for e in audit["edge_evidence"][0]["reference"]["is_a_path"]],
                         ["SO:0001265", "SO:0002342"])
        self.assertTrue(gene_definition_has_locus_scope("A gene that codes for an RNA that can be translated into a protein."))
        self.assertTrue(gene_definition_has_locus_scope("A segment of genomic DNA that encodes a functional RNA."))
        for definition in ("A protein encoded by a gene.", "A gene product that regulates transcription.",
                           "A protein or a gene.", "A gene expression regulator.", "An RNA associated with a gene locus."):
            self.assertFalse(gene_definition_has_locus_scope(definition))

    def test_previously_imported_gene_with_product_definition_cannot_receive_a_new_edge(self):
        dag = EntityDAG(["Gene"])
        parent = {**node("old", "RNA gene class", kind="Gene"), "standard_ids": ["T:1"],
                  "origin": "kg_enrichment_reference_parent", "definition": "An RNA involved in gene expression."}
        dag.place(parent)
        before = set(dag.edges)
        target = node("new", "Gene locus", kind="Gene", references=[term("T:0", "Locus", parents=["T:1"])])
        # Even an updated authoritative entry cannot silently repair a mismatched local parent.
        official = term("T:1", "RNA gene class", definition="A gene that encodes a functional RNA.")
        staged, audit = place_verified(
            dag, target, request=model(suggestion=False, comparisons={"old": (True, False)}),
            parent_lookup=lookup_for(official),
        )
        self.assertIs(staged, dag)
        self.assertEqual(staged.edges, before)
        self.assertEqual(audit["status"], "needs_review")
        self.assertEqual(audit["typed_relation_reviews"][0]["review"]["relation_type"], "UNCERTAIN")
        self.assertEqual(audit["parent_fallback"]["candidates"][0]["status"],
                         "stored_parent_definition_does_not_establish_gene_scope")

    def test_reuses_existing_alias_via_two_hop_is_a_path(self):
        self.dag.place(node("existing", "Specialized lineage", aliases=["Specific Source Category"]))
        target = node("new", "Candidate", references=[term("T:0", "Candidate", parents=["T:1"])])
        source = lookup_for(
            term("T:1", "Unusable name", parents=["T:2"], definition=None),
            term("T:2", "Specific Source Category", parents=["T:3"]),
            term("T:3", "Another higher class"),
        )
        staged, audit = place_verified(self.dag, target, request=model(), parent_lookup=source)
        self.assertEqual(audit["status"], "inserted")
        self.assertIsNone(audit["generated_parent_id"])
        self.assertIn(("new", "existing"), staged.edges)
        self.assertNotIn("new", self.dag.nodes)
        self.assertEqual([kw.args[-1] for kw in source.call_args_list], ["T:1", "T:2"])
        edge, = audit["edge_evidence"]
        self.assertEqual(edge["support"], "external_parent_fallback")
        self.assertEqual(edge["reference"]["local_match"], "exact_label_or_synonym")
        self.assertEqual([step["parent_id"] for step in edge["reference"]["is_a_path"]], ["T:1", "T:2"])

    def test_specific_parent_replaces_root_and_needs_independent_import_scope(self):
        target = node("new", "Candidate", references=[term("T:0", "Candidate", parents=["T:1"])])
        parent = term("T:1", "Specific Source Category", parents=["T:2"])
        source = lookup_for(parent)
        staged, audit = place_verified(self.dag, target, request=model(suggestion=False), parent_lookup=source)
        self.assertEqual(staged.nodes[audit["generated_parent_id"]]["definition"], parent["definition"])
        self.assertEqual(staged.nodes[audit["generated_parent_id"]]["standard_ids"], ["T:1"])
        self.assertEqual({edge["support"] for edge in audit["edge_evidence"]},
                         {"external_parent_fallback", "schema_type_assignment"})
        source.assert_called_once_with("TestOntology", "T:1")

        staged, audit = place_verified(
            self.dag, target, request=model(suggestion=False, root_review=False), parent_lookup=source,
        )
        self.assertIs(staged, self.dag)
        self.assertEqual(audit["status"], "needs_review")
        self.assertEqual(audit["parent_fallback"]["status"], "import_scope_unverified")

    def test_component_or_annotated_process_never_becomes_a_parent(self):
        for kind, predicate in (("Protein", "part_of"), ("Pathway", "annotated_to")):
            with self.subTest(kind=kind):
                dag = EntityDAG([kind])
                anchor = term("T:0", "Sourced candidate", parents=["T:1"])
                anchor["parent_relations"] = [{"parent_id": "T:1", "predicate": predicate}]
                target = node("new", "Component or subprocess", kind=kind, references=[anchor])
                source = lookup_for(term("T:1", "Composition, not taxonomy"))
                staged, audit = place_verified(dag, target, request=model(), parent_lookup=source)
                source.assert_not_called()
                self.assertIs(staged, dag)
                self.assertEqual(audit["parent_fallback"]["ignored_relations"][0]["predicate"], predicate)

    def test_original_biomedical_edges_must_pass_typed_review(self):
        dag = EntityDAG(["Pathway"])
        dag.place(node("local", "Surface receptor signal", kind="Pathway"))
        target = node("new", "Intracellular signal", kind="Pathway")
        for relation in ("REVERSE_IS_A", "EQUIVALENT", "PART_OF", "RELATED", "UNCERTAIN"):
            with self.subTest(relation=relation):
                request = model(comparisons={"local": (True, False)}, typed={"new->local": relation})
                staged, audit = place_verified(dag, target, request=request, typed_review=True)
                self.assertIs(staged, dag)
                self.assertNotEqual(audit["status"], "inserted")
                self.assertEqual(audit["typed_relation_reviews"][0]["review"]["relation_type"], relation)

    def test_exact_alias_with_conflicting_source_id_requires_review(self):
        self.dag.place(node("existing", "Shared label"))
        target = node("new", "Candidate", references=[term("T:0", "Candidate", parents=["T:1"])])
        staged, audit = place_verified(
            self.dag, target, request=model(), parent_lookup=lookup_for(term("T:1", "Shared label")),
            known_standard_ids={"existing": ["T:2"]},
        )
        self.assertIs(staged, self.dag)
        self.assertEqual(audit["parent_fallback"]["candidates"][0]["status"],
                         "local_name_conflicts_with_authority_id")

    def test_external_parent_cannot_reintroduce_rejected_ancestor(self):
        self.dag.place(node("ancestor", "An incompatible class"))
        self.dag.place(node("parent", "Specific Source Category"), parent_ids=["ancestor"])
        target = node("new", "Candidate", references=[term("T:0", "Candidate", parents=["T:1"])])
        request = model(comparisons={"ancestor": (True, False)},
                        original_reviews={"existing:ancestor:parent": False})
        staged, audit = place_verified(
            self.dag, target, request=request,
            parent_lookup=lookup_for(term("T:1", "Specific Source Category")),
        )
        self.assertIs(staged, self.dag)
        self.assertEqual(audit["parent_fallback"]["status"], "ancestor_conflict")
        self.assertEqual(audit["parent_fallback"]["transitive_conflicts"][0]["isa_path"],
                         ["new", "parent", "ancestor"])

    def test_cap_and_generic_terms_cannot_create_a_biomedical_parent(self):
        target = node("new", "Candidate", references=[term("T:0", "Candidate", parents=["T:1", "T:2"])])
        source = lookup_for(term("T:1", "cell", parents=["T:3"]), term("T:2", "Specific Source Category"))
        staged, audit = place_verified(self.dag, target, request=model(), parent_lookup=source,
                                       parent_budget=1, parent_depth=2)
        source.assert_called_once()
        self.assertIs(staged, self.dag)
        self.assertEqual(audit["status"], "needs_review")
        self.assertTrue(audit["parent_fallback"]["budget_exhausted"])
        self.assertEqual(audit["parent_fallback"]["candidates"][0]["status"],
                         "non_specific_or_missing_definition")
