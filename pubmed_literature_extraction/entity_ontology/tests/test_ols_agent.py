"""Exercise adaptive tool selection and pair evidence at the placement boundary."""

from tempfile import TemporaryDirectory
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from entity_ontology.hierarchy.entity_reference import EntityReferenceLookup, evidence_for_pair
from entity_ontology.hierarchy.ols_reference import MAX_STEPS, OLSReferenceAgent
from entity_ontology.hierarchy.ontology_ols import OLS_SOURCES, OntologyOLS
from entity_ontology.hierarchy.ols_client import OLSClient, OLSError
from entity_ontology.hierarchy.entity_placement_dag import EntityDAG
from entity_ontology.hierarchy.placement import place_one, review_item
from entity_ontology.tests.test_ontology_ols import FakeOLS, concept, term
from entity_ontology.tests.test_reference_placement import PARENT, CHILD, prompt_fixture


class OLSAgentTests(TestCase):
    def test_planner_can_switch_source_after_a_miss_then_verify_the_two_endpoints(self):
        first = term("MONDO:1", "mondo", PARENT["name"])
        second = term("MONDO:2", "mondo", CHILD["name"], directParent=[first["iri"]])
        api = FakeOLS({PARENT["name"]: [first], CHILD["name"]: [second]}, first, second)
        mcp = Mock()
        mcp.search_classes.return_value = []
        catalog = OntologyOLS(get_json=api, mcp=mcp)
        dag = EntityDAG(["Disease"])
        dag.place(PARENT)

        def request(task):
            stage, _, payload = task
            if stage == "ols_reference_action":
                observations = payload["observations"]
                self.assertEqual(payload["questions"][0]["parent_id"], PARENT["id"])
                if not observations:
                    action = {"action": "search", "source": "PRO", "query": CHILD["name"]}
                elif len(observations) == 1:
                    self.assertEqual(observations[-1]["result"], {"transport": "mcp", "hits": []})
                    action = {"action": "verify", "source": "MONDO", "endpoint_id": CHILD["id"]}
                elif len(observations) == 2:
                    self.assertEqual(observations[-1]["result"]["terms"][0]["direct_parent_ids"], ["MONDO:1"])
                    action = {"action": "verify", "source": "MONDO", "endpoint_id": PARENT["id"]}
                else:
                    action = {"action": "stop"}
                return [{"id": "next", **action}]
            if stage == "incremental_reference_review":
                self.assertEqual(payload["entity_references"][0]["source"], "MONDO")
                return [{"id": payload["items"][0]["id"], "supported": True, "reason": "fixture"}]
            return prompt_fixture(stage, payload["items"])

        with TemporaryDirectory() as folder:
            agent = OLSReferenceAgent(request, catalog=catalog)
            decision = place_one(dag, CHILD, request=request, entity_lookup=EntityReferenceLookup(Path(folder), ols=agent))
        result = decision["entity_reference"]["result"]
        self.assertEqual(result["sources_checked"], ["PRO", "MONDO"])
        self.assertEqual(result["tool_calls"], 3)
        self.assertEqual(decision["action"], "as_child")
        self.assertEqual(decision["entity_reference"]["pair_sources"], {"existing:parent:parent": "MONDO"})

    def test_all_sources_are_available_in_the_same_loop_and_go_can_be_disabled(self):
        target = {**CHILD, "kind": "Pathway"}
        question = review_item("q", target, {**PARENT, "kind": "Pathway"})
        planner = Mock(side_effect=[
            [{"id": "next", "action": "search", "source": "GO", "query": "example"}],
            [{"id": "next", "action": "stop"}],
        ])
        catalog = Mock()
        outcome = OLSReferenceAgent(planner, allow_go=False, catalog=catalog).lookup(target, [question])
        self.assertEqual(outcome["status"], "UNCERTAIN")
        self.assertEqual(outcome["tool_calls"], 0)
        self.assertEqual(outcome["query_records"][0]["status"], "invalid_action")
        catalog.search_source.assert_not_called()
        self.assertEqual(set(OLSReferenceAgent(planner).sources), set(OLS_SOURCES))

    def test_tool_budget_and_no_fabricated_evidence(self):
        catalog = Mock()
        catalog.search_source.return_value = {"hits": [{"term_id": "PR:1"}], "transport": "mcp"}
        def plan(task):
            return [{"id": "next", "action": "search", "source": "PRO",
                     "query": "synonym " + str(task[2]["remaining_steps"]),
                     "terms": [{"term_id": "PR:1"}], "relations": [{"predicate": "is_a"}]}]
        result = OLSReferenceAgent(plan, catalog=catalog).lookup(CHILD, [review_item("q", CHILD, PARENT)])
        self.assertEqual(result["tool_calls"], MAX_STEPS)
        self.assertEqual(result["reason"], "step_limit")
        self.assertEqual((result["terms"], result["relations"]), ([], []))

    def test_no_mixing_sources_or_creating_gene_hierarchy(self):
        q = review_item("q", CHILD, PARENT)
        result = {"source": "PRO, MONDO", "sources_checked": ["PRO", "MONDO"], "terms": [
            {"concept_id": "child", "source": "PRO", "term_id": "PR:1"},
            {"concept_id": "parent", "source": "MONDO", "term_id": "MONDO:1"}],
            "relations": [{"source": "PRO", "child_id": "PR:1", "parent_id": "MONDO:1", "predicate": "is_a"}]}
        self.assertIsNone(evidence_for_pair(q, result))
        self.assertFalse(OntologyOLS().verify_endpoint("PRO", concept("g", "TP53", "Gene"))["terms"])
        self.assertFalse(OntologyOLS().verify_endpoint("HGNC", concept("p", "p53", "Protein"))["terms"])

    def test_mcp_discovery_falls_back_to_ols_rest(self):
        item = term("CL:1", "cl", "Example cell")
        mcp = Mock()
        mcp.search_classes.side_effect = OLSError("unavailable")
        catalog = OntologyOLS(mcp=mcp, get_json=FakeOLS({"Example cell": [item]}, item))
        self.assertEqual(catalog.search_source("CL", "Example cell"),
                         {"transport": "rest", "hits": [{"term_id": "CL:1", "label": "Example cell"}]})

    def test_go_and_mondo_share_the_same_ols_mcp_search_tool(self):
        mcp = OLSClient(mode="mcp")
        catalog = OntologyOLS(mcp=mcp)
        examples = [
            ("GO", "go", "GO:0008150", "biological process"),
            ("MONDO", "mondo", "MONDO:0000001", "disease or disorder"),
        ]
        with patch.object(mcp, "discover", return_value={"transport": "mcp"}):
            for source, ontology, term_id, name in examples:
                with self.subTest(source=source), patch.object(
                    mcp, "_tool", return_value={"items": [term(term_id, ontology, name)]},
                ) as tool:
                    self.assertEqual(catalog.search_source(source, name),
                                     {"transport": "mcp", "hits": [{"term_id": term_id, "label": name}]})
                    tool.assert_called_once_with("searchClasses", {
                        "ontologyId": ontology, "query": name, "includeObsoleteEntities": False,
                        "pageNum": 0, "pageSize": mcp.page_size,
                    })

    def test_inspection_requires_an_observed_id_and_does_not_create_a_mapping(self):
        item = term("CL:1", "cl", "Example cell")
        mcp = Mock()
        mcp.search_classes.return_value = [item]
        catalog = OntologyOLS(mcp=mcp, get_json=FakeOLS({}, item))
        actions = [
            {"action": "inspect", "source": "CL", "term_id": "CL:999"},
            {"action": "search", "source": "CL", "query": "Example cell"},
            {"action": "inspect", "source": "CL", "term_id": "CL:1"},
            {"action": "stop"},
        ]
        plan = Mock(side_effect=[[{"id": "next", **action}] for action in actions])
        result = OLSReferenceAgent(plan, catalog=catalog).lookup(CHILD, [review_item("q", CHILD, PARENT)])
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(result["query_records"][0]["status"], "invalid_action")
        self.assertEqual(result["query_records"][2]["result"]["record"]["definition"], "Definition of Example cell.")
        self.assertEqual(result["terms"], [])

    def test_failed_partial_lookup_can_be_retried_from_the_cache(self):
        agent = Mock()
        agent.lookup.side_effect = [{"status": "REFERENCE_ONLY", "retryable": True},
                                    {"status": "REFERENCE_ONLY", "retryable": False}]
        with TemporaryDirectory() as folder:
            lookup = EntityReferenceLookup(Path(folder), ols=agent)
            self.assertTrue(lookup(CHILD, [])["retryable"])
            self.assertFalse(lookup(CHILD, [])["retryable"])
            self.assertFalse(lookup(CHILD, [])["retryable"])
        self.assertEqual(agent.lookup.call_count, 2)
