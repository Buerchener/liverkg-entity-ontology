import gzip
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cognitive_agent.relation_contract import normalize_relation_semantics
from cognitive_agent.schema.ontology import classify_evidence, relation_schema_valid
from cognitive_agent.schema.predicate_cards import match_relation_card
from cognitive_agent.schema.write_contract import MAIN_KG_WRITE_CONTRACT, METABOLITE_DISEASE, V2_MAIN_KG_WRITE_CONTRACT
from entity_ontology.backbone_hmdb import select_hmdb
from entity_ontology.backbone_pathways import PathwayCatalog, read_pathways


def xml_record(identifier, name, *, tissue="", xref="", protein="", disease="", taxonomy="Ceramides"):
    return f"""<metabolite><version>5.0</version><accession>{identifier}</accession><name>{name}</name>
      <chebi_id>{xref}</chebi_id><taxonomy><kingdom>Organic compounds</kingdom><class>Sphingolipids</class>
      <sub_class>{taxonomy}</sub_class></taxonomy>
      <biological_properties><tissue_locations><tissue>{tissue}</tissue></tissue_locations></biological_properties>
      <protein_associations>{protein}</protein_associations><diseases>{disease}</diseases></metabolite>"""


class BackboneSourcesTests(TestCase):
    def test_namespaced_hmdb_gzip_preserves_baseline_and_applies_both_approved_routes(self):
        from_disease = "<disease><name>Cirrhosis</name><references><reference><pubmed_id>57</pubmed_id></reference></references></disease>"
        transporter = "<protein><gene_name>ABC</gene_name><protein_type>Transporter</protein_type></protein>"
        unknown = transporter.replace("Transporter", "Unknown")
        records = [
            xml_record("HMDB01", "Baseline"),
            xml_record("HMDB02", "Liver ceramide", tissue="Liver", xref="20"),
            xml_record("HMDB03", "Outside the liver", tissue="Skin", xref="20"),
            xml_record("HMDB04", "Liver disease match", protein=transporter, disease=from_disease, taxonomy="Other"),
            xml_record("HMDB05", "Unspecified binding", protein=unknown, disease=from_disease, taxonomy="Other"),
        ]
        data = ('<hmdb xmlns="http://www.hmdb.ca">' + "".join(records) + "</hmdb>").encode()
        source = {"version": "HMDB 5.0", "uncompressed_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        path_id = "Reactome:R-HSA-428157"
        catalog = PathwayCatalog({}, {}, set(), {}, {"CHEBI:20": {path_id}}, {}, set(), {path_id})
        genes = {"NCBIGene:1": {"gene_symbol": "ABC", "protein_ids_from_disgenet": "M1"}}
        with TemporaryDirectory() as directory:
            archive = Path(directory) / "hmdb.xml.gz"
            archive.write_bytes(gzip.compress(data))
            selection = select_hmdb(archive, source, {"HMDB:HMDB01"}, genes, {"cirrhosis": "UMLS:test"},
                                    catalog, {path_id})
            self.assertEqual(selection.scanned, 5)
            by_id = {row.identifier: row for row in selection.records}
            self.assertEqual(set(by_id), {"HMDB:HMDB01", "HMDB:HMDB02", "HMDB:HMDB04"})
            ceramide = by_id["HMDB:HMDB02"]
            self.assertEqual(ceramide.properties["selection_routes"], ["liver_pathway_and_lipid_class"])
            self.assertEqual(ceramide.pathways[path_id], {("Reactome", "CHEBI:20")})
            self.assertEqual(ceramide.diseases, {})
            strict = by_id["HMDB:HMDB04"]
            self.assertEqual(strict.genes["NCBIGene:1"]["hmdb_protein_roles"], ["Transporter"])
            self.assertEqual(strict.diseases["UMLS:test"]["hmdb_pmids"], ["57"])
            with self.assertRaisesRegex(ValueError, "handoff checksum"):
                select_hmdb(archive, {**source, "sha256": "0" * 64}, {"HMDB:HMDB01"}, genes, {}, catalog, {path_id})

    def test_pathways_stop_at_the_ancestors_of_seed_pathways(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "ReactomePathways.txt": "R-HSA-1\tRoot\tHomo sapiens\nR-HSA-2\tSeed\tHomo sapiens\n"
                                        "R-HSA-3\tSibling\tHomo sapiens\nR-MMU-2\tOther species\tMus musculus\n",
                "ReactomePathwaysRelation.txt": "R-HSA-1\tR-HSA-2\nR-HSA-1\tR-HSA-3\n",
                "Pathways2GoTerms_human.txt": "Identifier\tName\tGO_Term\nR-HSA-2\tSeed\tGO:0000002\n",
                "ChEBI2Reactome.txt": "123\tR-HSA-2\turl\tSeed\tTAS\tHomo sapiens\n",
                "kegg_compound_pathways.tsv": "path:map00120\tcpd:C12345\n",
                "kegg_br08901.json": json.dumps({"name": "br08901", "children": [
                    {"name": "Metabolism", "children": [{"name": "Lipids", "children": [
                        {"name": "00120 Primary bile acid biosynthesis"}, {"name": "00600 Sphingolipids"}]}]},
                    {"name": "Unused category", "children": [{"name": "09999 Other"}]},
                ]}),
            }
            for name, text in files.items():
                (root / name).write_text(text, encoding="utf-8")
            catalog = read_pathways(root, {"Reactome:R-HSA-2", "KEGG:hsa00120"})
            self.assertEqual({catalog.names[key] for key in catalog.kegg_groups}, {"Metabolism", "Lipids"})
            self.assertEqual(catalog.parents["Reactome:R-HSA-2"], {"Reactome:R-HSA-1"})
            self.assertNotIn("Reactome:R-HSA-3", catalog.names)
            self.assertEqual(catalog.go["Reactome:R-HSA-2"], {"GO:0000002"})
            self.assertEqual(catalog.compounds["KEGG.COMPOUND:C12345"], {"KEGG:hsa00120"})

    def test_metabolite_disease_semantics_and_v2_compatibility(self):
        legacy = ("ASSOCIATED_WITH", "Metabolite", "Disease")
        current = (METABOLITE_DISEASE, "Metabolite", "Disease")
        self.assertIn(legacy, V2_MAIN_KG_WRITE_CONTRACT)
        self.assertNotIn(legacy, MAIN_KG_WRITE_CONTRACT)
        self.assertEqual(MAIN_KG_WRITE_CONTRACT[current], "relationship_id")
        self.assertTrue(relation_schema_valid(*current))
        self.assertFalse(relation_schema_valid(METABOLITE_DISEASE, "Gene", "Disease"))
        self.assertEqual(classify_evidence(*current, source="HMDB")[0], "contextual_background")
        self.assertEqual(classify_evidence(*current, source="literature")[0], "direct_disease_evidence")
        normalized = normalize_relation_semantics({
            "predicate": legacy[0], "subject_type": legacy[1], "object_type": legacy[2], "direction": "positive",
        })
        self.assertEqual(normalized["predicate"], METABOLITE_DISEASE)
        self.assertEqual(normalized["association_sign"], "POSITIVE")
        self.assertEqual(match_relation_card(*current, "Cholate was associated with cirrhosis.")["match"], "EXPLICIT")
