"""Load or remove a literature run on the exact v3 backbone it aligned to."""

from collections import defaultdict
from itertools import batched
import re
from urllib.parse import urlsplit

from cognitive_agent.schema.write_contract import MAIN_KG_WRITE_CONTRACT

from ..backbone_schema import NODE_KEYS, ROOT_CLASS_ID
from .neo4j_load import BATCH_SIZE, CONSTRAINTS, _props, check_target


RUN_CONSTRAINT = "CREATE CONSTRAINT livertrail_run_id IF NOT EXISTS FOR (n:LiteratureRun) REQUIRE n.run_id IS UNIQUE"


def direct_target(uri, database):
    check_target(uri, database)
    parsed = urlsplit(uri)
    if parsed.scheme != "bolt" or parsed.username is not None or parsed.password is not None or parsed.query:
        raise ValueError("Use a direct bolt:// loopback URI with credentials in the environment.")


def validate_v3_plan(plan):
    run_id = plan["run_id"]
    snapshot_id = plan["backbone"].get("backbone_snapshot_id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
        raise ValueError("The literature graph needs a v3 backbone snapshot ID.")
    for name in ("concepts", "mentions", "evidence", "relations"):
        rows = plan[name]
        if any(row["run_id"] != run_id or not row["uid"].startswith(f"{run_id}/") for row in rows):
            raise ValueError(f"graph/{name}.jsonl contains a different run.")
        if len({row["uid"] for row in rows}) != len(rows):
            raise ValueError(f"graph/{name}.jsonl has duplicate IDs.")

    concepts = {row["uid"]: row["kind"] for row in plan["concepts"]}
    if not set(concepts.values()).issubset(NODE_KEYS):
        raise ValueError("A concept has an unknown v3 biomedical type.")
    links = plan["same_as"]
    if len({row["concept_uid"] for row in links}) != len(links):
        raise ValueError("A concept has multiple backbone targets.")
    if any(concepts.get(row["concept_uid"]) != row["label"] for row in links):
        raise ValueError("A concept and its backbone target have different entity types.")
    for relation in plan["relations"]:
        signature = (relation["predicate"], concepts[relation["subject_uid"]], concepts[relation["object_uid"]])
        if signature not in MAIN_KG_WRITE_CONTRACT:
            raise ValueError(f"Unsupported v3 literature relationship signature {signature!r}.")


def statements_v3(plan):
    yield "articles", (
        "UNWIND $rows AS row MATCH (run:LiteratureRun {run_id: $run_id}) "
        "MERGE (a:Article {pmid: row.pmid}) "
        "ON CREATE SET a.source = 'literature', a.title = row.title "
        "MERGE (a)-[r:IN_RUN {run_id: $run_id}]->(run) SET r.source = 'literature'"
    ), plan["articles"]

    yield "concepts", (
        "UNWIND $rows AS row MERGE (c:Concept {uid: row.uid}) "
        "SET c += row.props, c.source = 'literature'"
    ), [{"uid": row["uid"], "props": _props(row, "uid")} for row in plan["concepts"]]

    yield "concept_hierarchy", (
        "UNWIND $rows AS row MATCH (c:Concept {uid: row.child_uid}), (p:Concept {uid: row.parent_uid}) "
        "MERGE (c)-[r:IS_A {run_id: $run_id}]->(p) SET r.support = row.support, r.source = 'literature'"
    ), plan["is_a"]

    same_as = defaultdict(list)
    for row in plan["same_as"]:
        same_as[row["label"]].append(row)
    for label, rows in same_as.items():
        key = NODE_KEYS[label]
        yield f"same_as:{label}", (
            "UNWIND $rows AS row MATCH (c:Concept {uid: row.concept_uid}) "
            f"MATCH (n:{label} {{{key}: row.node_id, backbone_version: 'v3'}}) "
            "MERGE (c)-[r:SAME_AS {run_id: $run_id}]->(n) SET r.source = 'literature'"
        ), rows

    yield "inherit_backbone_hierarchy", (
        "UNWIND $rows AS row "
        "MATCH (c:Concept {uid: row.concept_uid})-[:SAME_AS {run_id: $run_id}]->(n) "
        "MATCH (n)-[:IS_A|SUBPATHWAY_OF]->(p) "
        "WITH c, collect(DISTINCT p) AS parents "
        "FOREACH (parent IN parents | "
        "MERGE (c)-[r:IS_A {run_id: $run_id}]->(parent) SET r.source = 'literature')"
    ), plan["same_as"]
    children = {row["child_uid"] for row in plan["is_a"]}
    linked = {row["concept_uid"]: row for row in plan["same_as"]}
    yield "unlinked_type_roots", (
        "UNWIND $rows AS row MATCH (c:Concept {uid: row.uid}) "
        "MATCH (p:BackboneClass {class_id: row.class_id}) "
        "MERGE (c)-[r:IS_A {run_id: $run_id}]->(p) SET r.source = 'literature'"
    ), [{"uid": row["uid"], "class_id": f"LiverTrail:{row['kind']}"}
        for row in plan["concepts"] if row["uid"] not in children and row["uid"] not in linked]

    yield "mentions", (
        "UNWIND $rows AS row MATCH (c:Concept {uid: row.concept_uid}), (a:Article {pmid: row.pmid}) "
        "MERGE (m:Mention {uid: row.uid}) SET m += row.props, m.source = 'literature' "
        "MERGE (m)-[f:REFERS_TO {run_id: $run_id}]->(c) SET f.source = 'literature' "
        "MERGE (m)-[i:IN_ARTICLE {run_id: $run_id}]->(a) SET i.source = 'literature'"
    ), [{**row, "props": _props(row, "uid", "concept_uid")} for row in plan["mentions"]]

    concepts = defaultdict(list)
    anchored = defaultdict(list)
    for row in plan["relations"]:
        predicate = row["predicate"]
        start_uid, end_uid = row["subject_uid"], row["object_uid"]
        props = _props(row, "subject_uid", "object_uid", "predicate")
        props["source"] = "literature"
        concepts[predicate].append({"uid": row["uid"], "start": start_uid, "end": end_uid, "props": props})
        if start_uid in linked and end_uid in linked:
            start, end = linked[start_uid], linked[end_uid]
            signature = (predicate, start["label"], end["label"])
            anchored[signature].append({
                "id": f"LITERATURE:{row['uid']}", "start": start["node_id"], "end": end["node_id"], "props": props,
            })

    for predicate, rows in concepts.items():
        yield f"concept_relations:{predicate}", (
            "UNWIND $rows AS row MATCH (s:Concept {uid: row.start}), (o:Concept {uid: row.end}) "
            f"MERGE (s)-[r:{predicate} {{uid: row.uid}}]->(o) SET r += row.props"
        ), rows

    for (predicate, start, end), rows in anchored.items():
        id_key = MAIN_KG_WRITE_CONTRACT[predicate, start, end]
        yield f"backbone_relations:{predicate}", (
            "UNWIND $rows AS row "
            f"MATCH (s:{start} {{{NODE_KEYS[start]}: row.start, backbone_version: 'v3'}}), "
            f"(o:{end} {{{NODE_KEYS[end]}: row.end, backbone_version: 'v3'}}) "
            f"MERGE (s)-[r:{predicate} {{{id_key}: row.id}}]->(o) SET r += row.props"
        ), rows

    yield "evidence", (
        "UNWIND $rows AS row MATCH (a:Article {pmid: row.pmid}), "
        "(s:Concept {uid: row.subject_uid}), (o:Concept {uid: row.object_uid}) "
        "MERGE (e:Evidence {uid: row.uid}) SET e += row.props, e.source = 'literature' "
        "MERGE (e)-[i:IN_ARTICLE {run_id: $run_id}]->(a) SET i.source = 'literature' "
        "MERGE (e)-[l:ABOUT {role: 'subject', run_id: $run_id}]->(s) SET l.source = 'literature' "
        "MERGE (e)-[r:ABOUT {role: 'object', run_id: $run_id}]->(o) SET r.source = 'literature'"
    ), [{**row, "props": _props(row, "uid", "subject_uid", "object_uid")} for row in plan["evidence"]]


def _require_snapshot(tx, snapshot_id=None):
    root = tx.run(
        "MATCH (n:BackboneClass {class_id: $reference, backbone_version: 'v3'}) RETURN n.snapshot_id AS snapshot",
        reference=ROOT_CLASS_ID,
    ).single()
    if root is None or not root["snapshot"] or snapshot_id and root["snapshot"] != snapshot_id:
        raise ValueError("The local database does not have the required v3 backbone snapshot.")


def _write(tx, plan):
    run_id, snapshot_id = plan["run_id"], plan["backbone"]["backbone_snapshot_id"]
    _require_snapshot(tx, snapshot_id)
    record = tx.run(
        "MERGE (r:LiteratureRun {run_id: $run_id}) "
        "ON CREATE SET r.source = 'literature', r.snapshot_id = $snapshot_id, r.graph_id = $graph_id "
        "RETURN r.source AS source, r.graph_id AS id",
        run_id=run_id, snapshot_id=snapshot_id, graph_id=plan["graph_id"],
    ).single(strict=True)
    if record["source"] != "literature" or record["id"] != plan["graph_id"]:
        raise ValueError("The run ID already belongs to a different literature graph.")
    counts = {}
    for name, statement, rows in statements_v3(plan):
        counts[name] = 0
        for chunk in batched(rows, BATCH_SIZE):
            written = tx.run(statement + " RETURN count(*) AS written", run_id=run_id, rows=list(chunk))
            count = written.single(strict=True)["written"]
            if count != len(chunk):
                raise ValueError(f"{name} could not resolve every input row. The run was rolled back.")
            counts[name] += count
    return counts


def load_v3(session, plan):
    session.execute_read(_require_snapshot, plan["backbone"]["backbone_snapshot_id"])
    for statement in (*CONSTRAINTS, RUN_CONSTRAINT):
        session.run(statement).consume()
    return session.execute_write(_write, plan)


def _remove(tx, run_id, dry_run):
    _require_snapshot(tx)
    record = tx.run(
        "MATCH (r:LiteratureRun {run_id: $run_id, source: 'literature'}) "
        "OPTIONAL MATCH (a:Article)-[:IN_RUN]->(r) RETURN collect(DISTINCT a.pmid) AS pmids",
        run_id=run_id,
    ).single(strict=True)
    match_edges = "MATCH ()-[r {run_id: $run_id, source: 'literature'}]->() "
    match_nodes = (
        "MATCH (n {run_id: $run_id, source: 'literature'}) "
        "WHERE n:Concept OR n:Mention OR n:Evidence OR n:LiteratureRun "
    )
    match_articles = (
        "MATCH (a:Article {source: 'literature'}) WHERE a.pmid IN $pmids "
        "AND NOT EXISTS { MATCH (a)-[:IN_RUN]->(r:LiteratureRun) WHERE r.run_id <> $run_id } "
        if dry_run else
        "MATCH (a:Article {source: 'literature'}) WHERE a.pmid IN $pmids AND NOT (a)--() "
    )
    counts = {}
    for name, alias, match in (("relationships", "r", match_edges), ("nodes", "n", match_nodes),
                               ("articles", "a", match_articles)):
        statement = match + ("" if dry_run else f"DELETE {alias} ") + "RETURN count(*) AS count"
        counts[name] = tx.run(statement, run_id=run_id, pmids=record["pmids"]).single(strict=True)["count"]
    return counts


def unload_run(run_id, *, uri, database, user, password, dry_run=False, driver_factory=None):
    direct_target(uri, database)
    if not password:
        raise ValueError("Set NEO4J_PASSWORD for the local v3 database.")
    if driver_factory is None:
        from neo4j import GraphDatabase

        driver_factory = GraphDatabase.driver
    with driver_factory(uri, auth=(user, password)) as driver:
        with driver.session(database=database) as session:
            transact = session.execute_read if dry_run else session.execute_write
            counts = transact(_remove, run_id, dry_run)
    return {"run_id": run_id, "database": database, "dry_run": dry_run, "counts": counts}
