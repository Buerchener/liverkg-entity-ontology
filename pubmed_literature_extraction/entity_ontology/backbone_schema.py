"""Keys shared by the v3 snapshot, Neo4j and literature loader."""

from cognitive_agent.schema.write_contract import MAIN_KG_WRITE_CONTRACT


NODE_KEYS = {
    "Disease": "disease_id", "Gene": "gene_id", "Protein": "protein_id",
    "Pathway": "pathway_id", "Metabolite": "metabolite_id",
    "Tissue": "tissue_id", "CellType": "cell_type_id",
}
ROOT_CLASS_ID = "LiverTrail:Entity"
V3_NODE_KEYS = {**NODE_KEYS, "BackboneClass": "class_id", "ExternalReference": "xref"}
V3_RELATION_KEYS = {
    **MAIN_KG_WRITE_CONTRACT,
    **{("IS_A", label, "BackboneClass"): "hierarchy_id" for label in (*NODE_KEYS, "BackboneClass")},
    ("SUBPATHWAY_OF", "Pathway", "Pathway"): "hierarchy_id",
    ("CROSS_REFERENCE", "Pathway", "ExternalReference"): "xref_id",
    ("CROSS_REFERENCE", "Pathway", "Pathway"): "xref_id",
    ("PARTICIPATES_IN", "Metabolite", "Pathway"): "relationship_id",
    ("STAGE_DIFFERENTIAL", "Gene", "Disease"): "transition_id",
}
SNAPSHOT_VERSION = "livertrail-backbone-v3-jsonl-v1"
