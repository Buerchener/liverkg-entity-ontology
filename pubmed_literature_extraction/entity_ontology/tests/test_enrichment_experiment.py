"""Keep validated candidates distinct from published ontology edges."""

import unittest

from entity_ontology.enrichment.experiment import _metrics


class EvidenceComparisonTests(unittest.TestCase):
    def test_accepted_fallback_without_insert_still_counts_as_evidence(self):
        term = {"term_id": "CL:2"}
        edge = {
            "child_id": "kg-1", "parent_id": "intermediate", "support": "external_parent_fallback",
            "reference": {"parent_term": term},
        }
        root = {
            "child_id": "intermediate", "parent_id": "type:CellType", "support": "schema_type_assignment",
        }
        placement = {
            "proposal": {"concept_id": "kg-1"},
            "edge_evidence": [edge, root],
            "parent_fallback": {
                "status": "selected", "lookup_attempts": 1, "lookups": [{"status": "verified"}],
                "candidates": [{"status": "verified", "term": term}],
            },
        }
        withheld = {"status": "needs_review", "reason": "ambiguity", "kg": {"kind": "CellType"},
                    "placement": placement}
        pending = _metrics([withheld])
        self.assertEqual(pending["records_with_confirmed_biomedical_parent"], 1)
        self.assertEqual(pending["confirmed_biomedical_parent_pairs"], 1)
        self.assertEqual(pending["records_with_confirmed_biomedical_parent_and_not_inserted"], 1)
        self.assertEqual(pending["new_edges"]["strict_biomedical"], 0)

        inserted = {
            **withheld, "status": "inserted", "mapping": {"ontology_node_id": "kg-1"},
            "realignment": {"alignment": {"decision_status": "ALIGNED"}},
        }
        reported = _metrics([inserted], [edge, root], ["kg-1", "intermediate"])
        self.assertEqual(reported["new_edges"]["kg_to_schema_root"], 0)
        self.assertEqual(reported["new_edges"]["intermediate_to_schema_root"], 1)
        self.assertEqual(reported["new_edges"]["strict_biomedical"], 1)
        self.assertEqual(reported["intermediate_parents_inserted"], 1)

    def test_rejected_partial_edges_and_child_only_edges_do_not_confirm_target_parent(self):
        wrong_direction = {"child_id": "old", "parent_id": "new", "review": {"supported": True}}
        partial = {
            "child_id": "new", "parent_id": "broad", "review": {"supported": True},
            "typed_relation_review": {"relation_type": "RELATED_TO", "same_endpoint_scope": True,
                                      "informative": True},
        }
        blocked = {
            "status": "ancestor_conflict", "selected_parent_id": "q",
            "candidates": [{"id": "q", "status": "verified", "term": {"term_id": "CL:2"}}],
        }
        row = {
            "status": "needs_review", "reason": "conflict", "kg": {"kind": "CellType"},
            "placement": {"proposal": {"concept_id": "new"}, "edge_evidence": [wrong_direction, partial],
                          "parent_fallback": blocked},
        }
        result = _metrics([row])
        self.assertEqual(result["records_with_confirmed_biomedical_parent"], 0)
        self.assertEqual(result["records_with_independently_affirmed_parent_blocked_at_scope_or_graph_validation"], 1)

    def test_ols_denominators_exclude_invalid_stopped_and_cached_actions(self):
        audit = {"status": "REFERENCE_ONLY", "tool_calls": 3, "query_records": [
            {"status": "observed", "result": {"hits": [{"term_id": "CL:19"}]}},
            {"status": "observed", "result": {"record": None}},
            {"status": "lookup_failed"},
            {"status": "invalid_action", "error": "Already attempted; change the query"},
            {"status": "stopped"},
            {"status": "reused", "result": {"terms": [{"term_id": "CL:19"}]}},
        ]}
        observations = [
            {"source": "CL", "result": {"terms": []}},
            {"source": "CL", "status": "ambiguous_source_ids"},
            {"source": "CL", "action": "inspect", "result": {"record": {"term_id": "CL:10"}}},
        ]
        row = {"status": "needs_review", "reason": "confirmation absent",
               "kg": {"kind": "CellType", "reference_terms": [{"source": "CL"}]},
               "source_audit": {"observations": observations},
               "placement": {"reference_audits": [{"result": audit}]}}
        result = _metrics([row])["ols"]
        tool = result["planner_tool"]
        self.assertEqual(tool["nonempty_return_rate"], 1 / 3)
        self.assertEqual(tool["invalid_actions"], 1)
        self.assertEqual(tool["duplicate_actions"], 1)
        self.assertEqual(tool["stop_actions"], 1)
        self.assertEqual(tool["reused_actions"], 1)
        self.assertEqual(tool["lookup_failures"], 1)
        self.assertEqual(result["kg_grounding"]["definition_lookup"]["nonempty_return_rate"], 0)
        self.assertEqual(result["kg_grounding"]["exact_seed_inspections"]["nonempty_return_rate"], 1)
        self.assertEqual(result["kg_grounding"]["records_with_reference_seeds_fraction"], 1)


if __name__ == "__main__":
    unittest.main()
