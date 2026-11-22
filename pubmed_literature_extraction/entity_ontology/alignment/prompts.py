"""Versioned, case-independent prompts; benchmark before changing the default.

Design references are recorded in PROMPT_STUDY.md. The baseline prompt is copied
verbatim from the original local implementation, not from an external project.
"""
import json
from pathlib import Path


_DIRECTORY = Path(__file__).with_name("prompts")
LEGACY_POLICY = (_DIRECTORY / "legacy.txt").read_text(encoding="utf-8")
SECTIONS_POLICY = (_DIRECTORY / "sections.txt").read_text(encoding="utf-8")

CITATION_FIELDS = ["name", "definition", "aliases", "standard_ids", "entity_level_evidence"]
ASSESSMENT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["rows"],
    "properties": {"rows": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "relation_type", "confidence", "kg_entity_level",
                     "ontology_entity_level", "same_scope", "rationale", "evidence"],
        "properties": {
            "id": {"type": "string", "description": "Copy the current items[].id exactly."},
            "relation_type": {"type": "string", "enum": ["MAPS_TO", "INSTANCE_OF", "CLASS_SUBSUMPTION", "UNMATCHED", "UNCERTAIN"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "kg_entity_level": {"type": "string", "enum": ["class", "individual", "unknown"]},
            "ontology_entity_level": {"type": "string", "enum": ["class", "individual", "unknown"]},
            "same_scope": {"type": "boolean"}, "rationale": {"type": "string", "minLength": 1},
            "evidence": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["side", "field", "quote"],
                "properties": {
                    "side": {"type": "string", "enum": ["kg", "ontology"]},
                    "field": {"type": "string", "enum": CITATION_FIELDS},
                    "quote": {"type": "string", "minLength": 1,
                              "description": "Exact substring of the named LOCAL field, never external text."},
                },
            }},
        },
    }}},
}

# Schema is supplied in the prompt; the API response_format stays json_object.
# This is intentionally a prompt-only comparison, not constrained decoding.
SCHEMA_POLICY = (
    SECTIONS_POLICY.split("EXACT OUTPUT CONTRACT\n", 1)[0]
    + "OUTPUT JSON SCHEMA\nReturn an instance of this schema, not the schema itself.\n"
    + json.dumps(ASSESSMENT_SCHEMA, ensure_ascii=False, separators=(",", ":"))
    + "\nCHECK\nExactly one row for each items[].id; no missing or additional IDs. "
      "Every quote must be found verbatim in its designated LOCAL field. "
      "Do not add citations to external_reference. No Markdown or commentary outside the JSON.\n"
)

GROUNDED_POLICY = (_DIRECTORY / "grounded.txt").read_text(encoding="utf-8")
VERIFIED_POLICY = (_DIRECTORY / "verified.txt").read_text(encoding="utf-8")

PROMPT_VARIANTS = {"legacy": LEGACY_POLICY, "sections": SECTIONS_POLICY,
                   "schema": SCHEMA_POLICY, "grounded": GROUNDED_POLICY,
                   "verified": VERIFIED_POLICY}

# Selected before the held-out run; its frozen text passed the held-out check.
# See PROMPT_STUDY.md. Legacy remains available for reproducible comparisons.
DEFAULT_PROMPT = "verified"
