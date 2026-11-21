"""Use reviewed equivalence pairs to merge concepts and preserve their evidence."""

from concurrent.futures import ThreadPoolExecutor

from ..model import JSON_RETRY, endpoint, key
from ..storage import atomic_json, read_json
from .retrieve_pairs import DEFAULT_KNN_K, retrieve_pairs


EQUIV = """Judge each enumerated pair for strict concept equivalence using the candidate names, types, definitions and observed context. Merge only for the same biomedical referent at the same granularity. Relatedness, anatomical containment, biological role, association, distinct severity, and broader/narrower are separate. An abbreviation needs contextual support. If doubtful use uncertain. Return JSON object with rows array, one per pair id: id, verdict (equivalent/distinct/uncertain), reason (brief Chinese)."""
INDEXED_EQUIV = EQUIV + ' Copy each pair id as a quoted string exactly as supplied (for example "p1").'
PAIR_BATCH_SIZE = 10


def indexed_equivalence(items, session):
    def request_batch(batch):
        indexed = {f"p{i}": item for i, item in enumerate(batch, 1)}
        payload = {"items": [{**item, "id": alias} for alias, item in indexed.items()]}
        answers = session.req(("equivalence_indexed", INDEXED_EQUIV, payload))
        return [{**row, "id": indexed[row["id"]]["id"]} for row in answers]

    batches = [items[start:start + PAIR_BATCH_SIZE] for start in range(0, len(items), PAIR_BATCH_SIZE)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        return [row for result in pool.map(request_batch, batches) for row in result]



def equivalence_votes(items, *, session):
    if not items:
        return []
    conn, params = session.request_settings()
    active = {item["id"]: item for item in items}
    cached = {}
    disagreements = set()
    # Pair evidence can be identical even when retrieval changes the surrounding batch.
    for path in sorted((session.output / "calls").glob("equivalence-*.json")):
        journal = read_json(path)
        request = journal["request"]
        call_id = key([EQUIV, request, params, endpoint(conn)])
        if path.stem != f"equivalence-{call_id}" or journal["prompt"] not in (
            EQUIV, EQUIV + JSON_RETRY,
        ):
            continue
        responses = journal["response"]["rows"]
        if sorted(row["id"] for row in responses) != sorted(row["id"] for row in request["items"]):
            continue
        prior_items = {row["id"]: row for row in request["items"]}
        for row in responses:
            ident = row["id"]
            if active.get(ident) != prior_items[ident]:
                continue
            if ident in cached and cached[ident]["verdict"] != row["verdict"]:
                disagreements.add(ident)
            cached.setdefault(ident, row)
    # Conflicting saved judgments need a new call with the current batch.
    for ident in disagreements:
        cached.pop(ident)
    pending = [item for item in items if item["id"] not in cached]
    if len(items) > PAIR_BATCH_SIZE:
        answers = indexed_equivalence(pending, session)
    else:
        answers = session.batch("equivalence", EQUIV, pending, PAIR_BATCH_SIZE)
    cached.update({row["id"]: row for row in answers})
    return [cached[item["id"]] for item in items]


def deduplicate(concept_input, normalized, *, session, knn_k=DEFAULT_KNN_K):
    kept = {r["id"]: r for r in normalized if r["status"] == "keep"}
    mention_evidence = {
        mention["id"]: {
            "mention": mention["mention"],
            "sentence": article["sentences"][mention["context_id"]],
        }
        for article in concept_input["articles"]
        for mention in article["mentions"]
        if mention["id"] in kept
    }
    retrieval = retrieve_pairs(list(kept.values()), session.output, session.config, knn_k=knn_k)
    items = []
    for p in retrieval["pairs"]:
        sides = []
        for side in ("left", "right"):
            row = kept[p[side]]
            sides.append(
                {k: row[k] for k in ("name", "kind", "definition", "aliases")}
                | mention_evidence[row["id"]]
            )
        items.append(
            {"id": p["left"] + "|" + p["right"], "left": sides[0], "right": sides[1]}
        )
    votes = equivalence_votes(items, session=session)
    decisions = {}
    for v in votes:
        l, r = v["id"].split("|")
        if v["verdict"] not in ("equivalent", "distinct", "uncertain"):
            raise ValueError("Unknown equivalence judgment")
        compatible = kept[l]["kind"] == kept[r]["kind"]
        decisions[l, r] = {"relation": v["verdict"] if compatible else "distinct"}
        v["applied_verdict"] = decisions[l, r]["relation"]
    groups, conflicts = equivalent_groups(list(kept), decisions)
    concepts = []
    for members in groups:
        values = [kept[i] for i in members]
        terms = {}
        for r in values:
            for s in [r["name"], *r["aliases"]]:
                terms.setdefault(s.strip().casefold(), s.strip())

        # Prefer a supported expanded name for display; keep all model names and aliases for audit.
        def display_order(name):
            return (len(name.split()) > 1, len(name), name.casefold())

        name = max(terms.values(), key=display_order)
        representative = next(
            (r for r in values if r["name"].casefold() == name.casefold()), values[0]
        )
        concepts.append(
            {
                "id": "C-" + key(sorted(members))[:12],
                "name": name,
                "kind": representative["kind"],
                "definition": representative["definition"],
                "aliases": [s for s in terms.values() if s != name],
                "source_mentions": list(members),
                "origin": "literature_mention",
            }
        )
    atomic_json(session.output / "retrieval" / "retrieved_pairs.json", retrieval)
    atomic_json(
        session.output / "equivalence.json",
        {
            "pair_judgments": votes,
            "non_clique_components_held": conflicts,
            "concepts": concepts,
        },
    )
    return concepts


def equivalent_groups(members, decisions):
    """Build disjoint groups in input order; equivalence must hold for every pair."""
    # Conflicting votes (A=B, A=C, B!=C) are not used to pick an arbitrary merge.
    neighbors = {member: set() for member in members}
    for (left, right), decision in decisions.items():
        if decision["relation"] == "equivalent":
            neighbors[left].add(right)
            neighbors[right].add(left)
    groups, conflicts, seen = [], [], set()
    for member in members:
        if member in seen:
            continue
        connected, pending = {member}, [member]
        while pending:
            adjacent = neighbors[pending.pop()] - connected
            pending.extend(adjacent)
            connected.update(adjacent)
        component = tuple(node for node in members if node in connected)
        seen.update(component)
        if all(len(neighbors[node]) == len(component) - 1 for node in component):
            groups.append(component)
        else:
            conflicts.append(component)
            groups.extend((node,) for node in component)
    return groups, conflicts
