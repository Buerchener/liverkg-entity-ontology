"""Bounded cross-graph retrieval. A retrieved candidate is never an alignment.

Nodes use the construction pipeline's ``id/name/kind/definition/aliases``
fields, plus ``standard_ids``, ``parent_ids`` and ``is_root``. External IDs
must be namespaced; local Neo4j IDs and unqualified numbers are not evidence.
"""

from collections import Counter, defaultdict
import math
import re

import numpy as np

from ..entity_types import IN_SCOPE
from ..storage import norm


_NAMESPACES = {
    "ncbigene": "NCBIGene", "ncbi gene": "NCBIGene", "entrez": "NCBIGene",
    "entrezgene": "NCBIGene", "hgnc": "HGNC", "ensembl": "ENSEMBL",
    "uniprot": "UniProt", "uniprotkb": "UniProt", "string": "STRING",
    "umls": "UMLS", "mondo": "MONDO", "doid": "DOID", "mesh": "MeSH",
    "efo": "EFO", "ncit": "NCIT", "nci": "NCIT",
    "hpa_tissue": "HPA_TISSUE", "hpa_cell_type": "HPA_CELL_TYPE",
    "go": "GO", "chebi": "CHEBI", "pr": "PR", "cl": "CL", "uberon": "UBERON",
    "reactome": "Reactome", "kegg_pathway": "KEGG_PATHWAY", "kegg pathway": "KEGG_PATHWAY",
    "kegg_compound": "KEGG_COMPOUND", "kegg compound": "KEGG_COMPOUND",
    "kegg_gene": "KEGG_GENE", "kegg gene": "KEGG_GENE", "hmdb": "HMDB",
    "pubchem": "PubChem", "pubchem.compound": "PubChem", "ec": "EC",
}
_UPPER_LOCAL = {"UMLS", "MeSH", "UniProt", "ENSEMBL", "Reactome", "HMDB", "KEGG_COMPOUND", "NCIT", "PR"}
_NUMERIC_LOCAL = {"NCBIGene", "HGNC", "MONDO", "DOID", "GO", "CHEBI", "CL", "UBERON", "PubChem", "EFO"}
_STOP = {"a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or", "that", "the", "to", "with"}


def canonical_standard_id(value):
    """Conservatively canonicalize supported namespaces, preserving local IDs.

    Padding, versions and species prefixes stay intact. Unknown namespaces are
    ignored instead of accidentally treating an internal ``id:123`` as global.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    match = re.fullmatch(r"(GO|MONDO|DOID|CHEBI|PR|CL|UBERON)_(\d+)", value, re.I)
    if match:
        value = match.group(1) + ":" + match.group(2)
    if ":" not in value:
        return None
    namespace, local = value.split(":", 1)
    namespace = _NAMESPACES.get(namespace.strip().casefold())
    if not namespace or not local or local != local.strip() or any(c.isspace() for c in local):
        return None
    if namespace in _NUMERIC_LOCAL and not local.isascii():
        return None
    if namespace in _NUMERIC_LOCAL and not local.isdecimal():
        return None
    if namespace == "PR" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,35}", local):
        return None
    if namespace in _UPPER_LOCAL:
        local = local.upper()
    return namespace + ":" + local


def _standard_ids(node):
    return {ident for value in node.get("standard_ids", []) if (ident := canonical_standard_id(value))}


def _names(node):
    return {norm(value) for value in [node.get("name", ""), *node.get("aliases", [])]
            if isinstance(value, str) and value.strip()}


def _tokens(node):
    text = " ".join([*_names(node), node.get("definition", "") or ""])
    return {token for token in re.findall(r"[^\W_]+", norm(text))
            if len(token) > 1 and token not in _STOP}


def _embedding_text(node):
    return "\n".join([node.get("name", ""), node.get("kind", ""),
                      node.get("definition", ""), *node.get("aliases", [])])


def _unit_vectors(matrix, expected_rows):
    matrix = np.asarray(matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] != expected_rows or not np.isfinite(matrix).all():
        raise ValueError("Invalid alignment embedding matrix")
    lengths = np.linalg.norm(matrix, axis=1)
    if not np.isfinite(lengths).all() or np.any(lengths <= 0):
        raise ValueError("Invalid alignment embedding norm")
    return matrix / lengths[:, None]


class CandidateRetriever:
    """Index same-type nodes once, returning at most ``max_candidates`` per KG node.

    ``embeddings`` optionally accepts the existing ``Embeddings`` cache/provider.
    It is used only when no ID or exact name/alias candidate exists. Cosine
    scores, lexical overlap and hierarchy depth are retrieval cues, not proof.
    """

    def __init__(self, ontology_nodes, *, max_candidates=6, lexical_min_score=0.15,
                 embedding_min_score=0.5, embeddings=None):
        if type(max_candidates) is not int or not 1 <= max_candidates <= 50:
            raise ValueError("max_candidates must be an integer from 1 to 50")
        if not 0 <= lexical_min_score <= 1 or not -1 <= embedding_min_score <= 1:
            raise ValueError("Invalid retrieval similarity threshold")
        self.max_candidates = max_candidates
        self.lexical_min_score = lexical_min_score
        self.embedding_min_score = embedding_min_score
        self.embeddings = embeddings
        all_nodes = list(ontology_nodes)
        self.all_nodes = {node["id"]: node for node in all_nodes}
        if len(self.all_nodes) != len(all_nodes):
            raise ValueError("Duplicate ontology node IDs")
        self.nodes = {key: node for key, node in self.all_nodes.items()
                      if not node.get("is_root") and node.get("kind") in IN_SCOPE}
        self.by_type = defaultdict(list)
        self.by_name = defaultdict(set)
        self.by_id = defaultdict(set)
        self.by_token = defaultdict(set)
        self.tokens = {}
        self.depths = {}
        self.summaries = {}
        self._embedding_vectors = {}
        for key, node in self.nodes.items():
            kind = node["kind"]
            self.by_type[kind].append(key)
            for name in _names(node):
                self.by_name[kind, name].add(key)
            for ident in _standard_ids(node):
                self.by_id[kind, ident].add(key)
            self.tokens[key] = _tokens(node)
            for token in self.tokens[key]:
                self.by_token[kind, token].add(key)
        for ids in self.by_type.values():
            ids.sort()
        for key in self.nodes:
            self._depth(key, set())

    def _depth(self, key, visiting):
        if key in self.depths:
            return self.depths[key]
        if key in visiting:
            raise ValueError("Ontology hierarchy contains a cycle")
        parents = [p for p in self.all_nodes[key].get("parent_ids", []) if p in self.all_nodes]
        depth = max((self._depth(p, visiting | {key}) + 1 for p in parents), default=0)
        self.depths[key] = depth
        return depth

    def _lexical(self, node):
        kind, tokens = node["kind"], _tokens(node)
        overlaps = Counter(key for token in tokens for key in self.by_token[kind, token])
        size = len(self.by_type[kind])
        weights = {token: 1 + math.log((size + 1) / (len(self.by_token[kind, token]) + 1))
                   for token in tokens}
        for key in overlaps:
            common = tokens & self.tokens[key]
            # Weighted Dice penalizes a single generic word in a long definition.
            union = tokens | self.tokens[key]
            weight = lambda token: weights.get(token, 1 + math.log(
                (size + 1) / (len(self.by_token[kind, token]) + 1)))
            denominator = sum(weight(t) for t in tokens) + sum(weight(t) for t in self.tokens[key])
            score = 2 * sum(weight(t) for t in common) / denominator if union else 0.0
            if score >= self.lexical_min_score:
                yield key, {"method": "lexical", "score": round(score, 6),
                            "shared_tokens": sorted(common)}

    def _semantic(self, node):
        kind = node["kind"]
        ids = self.by_type[kind]
        if kind not in self._embedding_vectors:
            texts = {"ontology:" + key: _embedding_text(self.nodes[key]) for key in ids}
            self._embedding_vectors[kind] = _unit_vectors(self.embeddings.for_texts(texts), len(ids))
        vector = _unit_vectors(self.embeddings.for_texts({"kg:" + node["id"]: _embedding_text(node)}), 1)
        matrix = self._embedding_vectors[kind]
        if matrix.shape[1] != vector.shape[1]:
            raise ValueError("Alignment embedding dimensions differ")
        scores = matrix @ vector[0]
        ranked = sorted(range(len(ids)), key=lambda i: (-scores[i], ids[i]))[:self.max_candidates]
        for index in ranked:
            score = float(np.clip(scores[index], -1, 1))
            if score >= self.embedding_min_score:
                yield ids[index], {"method": "embedding", "score": round(score, 6)}

    def retrieve(self, kg_node):
        kind = kg_node.get("kind")
        reasons = defaultdict(list)
        exact_ids, exact_names = defaultdict(list), defaultdict(list)
        if kind in self.by_type:
            for ident in sorted(_standard_ids(kg_node)):
                for key in sorted(self.by_id[kind, ident]):
                    exact_ids[key].append(ident)
            for name in sorted(_names(kg_node)):
                for key in sorted(self.by_name[kind, name]):
                    exact_names[key].append(name)
            for key, ids in exact_ids.items():
                reasons[key].append({"method": "standard_id", "shared_ids": ids})
            for key, names in exact_names.items():
                reasons[key].append({"method": "exact_name_or_alias", "shared_names": names})
            if not reasons:
                for key, evidence in self._lexical(kg_node):
                    reasons[key].append(evidence)
                if self.embeddings is not None:
                    for key, evidence in self._semantic(kg_node):
                        reasons[key].append(evidence)

        def rank(key):
            methods = {reason["method"] for reason in reasons[key]}
            tier = 0 if "standard_id" in methods else 1 if "exact_name_or_alias" in methods else 2
            score = max((reason.get("score", 0) for reason in reasons[key]), default=0)
            return tier, -score, -self.depths[key], key

        ranked = sorted(reasons, key=rank)
        exact_count = len(set(exact_ids) | set(exact_names))
        self.summaries[kg_node["id"]] = {
            "total_candidates": len(ranked), "returned_candidates": min(len(ranked), self.max_candidates),
            "truncated": len(ranked) > self.max_candidates,
            "exact_candidate_count": exact_count, "ambiguous_exact": exact_count > 1,
            "entity_type": kind, "type_supported": kind in IN_SCOPE,
            "embedding_enabled": self.embeddings is not None,
            "policy": "retrieval_only_not_alignment",
        }
        return [{"ontology_node_id": key, "retrieval": reasons[key],
                 "specificity_depth": self.depths[key]} for key in ranked[:self.max_candidates]]

    def retrieval_summary(self, kg_node_id):
        return dict(self.summaries[kg_node_id])
