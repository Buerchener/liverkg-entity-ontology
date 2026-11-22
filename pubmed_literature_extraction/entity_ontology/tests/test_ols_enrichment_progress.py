"""Check bounded planner repair and narrow observed-label normalization."""

from unittest import TestCase
from unittest.mock import Mock

from entity_ontology.hierarchy.ols_reference import OLSReferenceAgent
from entity_ontology.hierarchy.ontology_ols import OntologyOLS
from entity_ontology.hierarchy.placement import review_item
from entity_ontology.tests.test_ontology_ols import FakeOLS, concept, term
from entity_ontology.tests.test_reference_placement import CHILD, PARENT


def grounded_target(predicate="is_a"):
    source_url = "https://example.test/child"
    edge = {"child_id": "MONDO:2", "parent_id": "MONDO:1", "predicate": predicate,
            "source": "MONDO", "parent_source": "MONDO", "source_url": source_url}
    return {**CHILD, "reference_terms": [{
        "source": "MONDO", "term_id": "MONDO:2", "name": CHILD["name"],
        "definition": CHILD["definition"], "source_url": source_url, "identity_evidence": {"id": "MONDO:2"},
        "direct_parent_ids": ["MONDO:1"], "parent_relations": [edge],
    }]}


def action(name, **data):
    return [{"id": "next", "action": name, **data}]


class OLSProgressTests(TestCase):
    def test_verified_parent_path_ends_planner_after_first_invalid_action(self):
        target = grounded_target()
        request = Mock(return_value=action("verify", source="MONDO", endpoint_id="unobserved"))
        catalog = Mock()
        result = OLSReferenceAgent(request, catalog=catalog, parent_fallback=True).lookup(
            target, [review_item("q", target, PARENT)],
        )
        request.assert_called_once()
        self.assertEqual(result["reason"], "parent_fallback_after_no_progress")
        self.assertEqual(result["planner_steps"], 1)
        self.assertEqual(result["invalid_actions"], 1)
        self.assertEqual(result["tool_calls"], 0)
        self.assertEqual(result["query_records"][-1]["status"], "invalid_action")
        self.assertEqual(result["relations"], [])
        self.assertEqual(catalog.mock_calls, [])

    def test_verified_parent_path_stops_redundant_reverification(self):
        target = grounded_target()
        request = Mock(return_value=action("verify", source="MONDO", endpoint_id=target["id"]))
        catalog = Mock()
        result = OLSReferenceAgent(request, catalog=catalog, parent_fallback=True).lookup(
            target, [review_item("q", target, PARENT)],
        )
        request.assert_called_once()
        self.assertEqual(result["reason"], "parent_fallback_after_no_progress")
        self.assertEqual(result["redundant_actions"], 1)
        self.assertEqual(result["query_records"][-1]["status"], "reused")
        self.assertEqual(catalog.mock_calls, [])

    def test_at_most_one_repair_after_duplicate_lookup_without_is_a_seed(self):
        for target, mode in ((grounded_target(), False), (grounded_target("part_of"), True)):
            with self.subTest(parent_fallback=mode):
                request = Mock(return_value=action("search", source="MONDO", query="same phrase"))
                catalog = Mock()
                catalog.search_source.return_value = {"hits": []}
                result = OLSReferenceAgent(request, catalog=catalog, parent_fallback=mode).lookup(
                    target, [review_item("q", target, PARENT)],
                )
                self.assertEqual(result["reason"], "no_progress_after_repair")
                self.assertEqual(result["planner_steps"], 3)
                self.assertEqual(result["invalid_actions"], 2)
                self.assertEqual(result["redundant_actions"], 2)
                self.assertEqual(result["tool_calls"], 1)
                catalog.search_source.assert_called_once_with("MONDO", "same phrase")

    def test_observed_id_conflicting_with_verified_endpoint_is_rejected(self):
        target = grounded_target()
        request = Mock(side_effect=[
            action("search", source="MONDO", query="overlapping phrase"),
            action("verify", source="MONDO", endpoint_id=target["id"], term_id="MONDO:9"),
        ])
        catalog = Mock()
        catalog.search_source.return_value = {"hits": [{"term_id": "MONDO:9", "label": target["name"]}]}
        result = OLSReferenceAgent(request, catalog=catalog, parent_fallback=True).lookup(
            target, [review_item("q", target, PARENT)],
        )
        self.assertEqual(result["reason"], "parent_fallback_after_no_progress")
        self.assertEqual(result["query_records"][-1]["status"], "invalid_action")
        self.assertEqual([row["term_id"] for row in result["terms"]], ["MONDO:2"])
        catalog.verify_observed_id.assert_not_called()


class ObservedSequenceOntologyLabelTests(TestCase):
    def test_so_label_separator_normalization_is_specific_to_observed_ids(self):
        external = term("SO:1", "so", "example_gene-class")
        disease = term("MONDO:2", "mondo", "example_gene-class")
        catalog = OntologyOLS(get_json=FakeOLS({}, external, disease))
        result = catalog.verify_observed_id("SO", concept("local", "Example-gene class", "Gene"), "SO:1")
        self.assertEqual(result["terms"][0]["match"], "normalized_so_label")
        self.assertEqual(result["terms"][0]["name"], "example_gene-class")
        candidate = concept("disease", "Example-gene class", "Disease")
        self.assertEqual(catalog.verify_observed_id("MONDO", candidate, "MONDO:2")["reason"],
                         "observed_id_scope_unconfirmed")

    def test_so_normalization_never_overrides_an_explicit_different_source_id(self):
        api = FakeOLS({}, term("SO:1", "so", "example_gene-class"))
        catalog = OntologyOLS(get_json=api)
        candidate = {**concept("local", "Example gene class", "Gene"), "standard_ids": ["SO:3"]}
        result = catalog.verify_observed_id("SO", candidate, "SO:1")
        self.assertEqual(result, {"terms": [], "reason": "observed_id_conflicts_with_supplied_id"})
        self.assertEqual(api.lookups, [])
