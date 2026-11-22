"""Regression checks for schema assignments and their provenance."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from entity_ontology.enrichment.auxiliary import auxiliary_context
from entity_ontology.enrichment.placement import _metabolite_scope, _pair
from entity_ontology.enrichment.pipeline import _case
from entity_ontology.hierarchy.entity_placement_dag import EntityDAG


class RootEvidenceTests(TestCase):
    def setUp(self):
        self.kg = {"id": "kg", "kind": "Metabolite", "name": "Example chemical", "aliases": [],
                   "standard_ids": ["CHEBI:111"], "definition": "A formally substituted compound."}
        self.definition = {"record_id": "CHEBI:111", "url": "https://example.org/111",
                           "definition": self.kg["definition"]}
        self.kg["definition_evidence"] = self.definition
        self.compound = {"id": 111, "chebi_accession": "CHEBI:111", "is_released": True,
                         "ontology_relations": {"outgoing_relations": [
                             {"init_id": 111, "relation_type": "has role", "final_id": 222}]},
                         "roles_classification": [{"chebi_accession": "CHEBI:222", "name": "plant metabolite",
                                                   "biological_role": True}]}
        self.role = {"iri": "http://purl.obolibrary.org/obo/CHEBI_222", "isObsolete": False,
                     "directAncestor": ["http://purl.obolibrary.org/obo/CHEBI_25212"]}

    def evidence(self):
        def get(url):
            return json.dumps(self.compound if "compound/" in url else self.role)
        with TemporaryDirectory() as tmp:
            return auxiliary_context(self.kg, self.definition, Path(tmp), get)[0]

    def test_independently_classified_role_supports_only_the_bound_compound(self):
        facts = self.evidence()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["relation"], "HAS_ROLE")
        self.assertFalse(_metabolite_scope(self.kg)["verified"])
        self.assertTrue(_metabolite_scope({**self.kg, "auxiliary_evidence": facts})["verified"])
        self.assertFalse(_metabolite_scope({**self.kg, "auxiliary_evidence": facts,
                                          "standard_ids": ["CHEBI:333"]})["verified"])

    def test_role_label_or_inverse_edge_alone_does_not_support_root(self):
        self.role["directAncestor"] = []
        self.assertEqual(self.evidence(), [])
        self.compound["ontology_relations"] = {"incoming_relations": [{"init_id": 222, "final_id": 111,
                                                                       "relation_type": "has role"}]}
        self.assertEqual(self.evidence(), [])

    def test_tissue_root_records_local_scope_only(self):
        dag = EntityDAG(["Tissue"])
        dag.place({"id": "vessel", "kind": "Tissue", "name": "A vessel",
                   "definition": "An anatomical conduit.", "aliases": []})
        pair = _pair(dag, "vessel", "type:Tissue")
        self.assertFalse(pair["strict"])
        self.assertTrue(pair["schema_type_assignment"])
        self.assertIn("anatomical", pair["schema_scope"]["meaning"])

    def test_source_scope_conflict_prevents_identity_or_placement(self):
        self.definition["scope_review_required"] = {"reason": "Specimen scope"}
        dag = EntityDAG(["Metabolite"])
        with TemporaryDirectory() as tmp, patch("entity_ontology.enrichment.pipeline.verify_concept") as verify:
            unchanged, row = _case(dag, self.kg, {}, Path(tmp), None, None, {}, use_ols=False)
        self.assertIs(unchanged, dag)
        self.assertEqual(row["reason"], "definition_scope_requires_review")
        verify.assert_not_called()
