import { el, articleLink, batchTitle, disclosure } from "./ui.js";

export const overviewSections = [
  ["summary", "Batch Results"],
  ["evidence", "Evidence Trace"],
  ["workflow", "Workflow"],
  ["sources", "Source Articles"],
];

const jump = (label, stage, selected, css) => el("a", {
  class: css,
  href: "#" + stage + (selected ? "/" + encodeURIComponent(selected) : ""),
}, label);

function excerpt(text, phrase) {
  if (!phrase) return [text];
  const at = text.toLowerCase().indexOf(phrase.toLowerCase());
  return at < 0 ? [text] : [text.slice(0, at), el("mark", {}, text.slice(at, at + phrase.length)), text.slice(at + phrase.length)];
}

function table(label, headings, rows, css) {
  return el("div", { class: "document-table-wrap" },
    el("table", { class: "data-table document-table " + css, "aria-label": label },
      el("thead", {}, el("tr", {}, headings.map(heading => el("th", { scope: "col" }, heading)))),
      el("tbody", {}, rows.map(([name, ...rest]) => el("tr", {}, el("th", { scope: "row" }, name),
        rest.map(value => el("td", {}, value)))))));
}

function section(id, heading, ...content) {
  return el("section", { class: "document-section", id, "aria-labelledby": id + "-heading" },
    el("h2", { id: id + "-heading" }, heading), content);
}

function foldedSection(id, label, ...content) {
  const details = disclosure(label, ...content);
  details.id = id;
  return details;
}

function renderTrace(concept, evidence, parent, nodes) {
  const [id, heading] = overviewSections[1];
  if (!concept || !evidence) return foldedSection(id, heading,
    el("p", { class: "muted" }, "This batch has no concepts linked to source evidence."));
  const sentence = evidence.context || evidence.evidence_contexts?.[0]?.text;
  const position = parent?.name || (nodes.has(concept.id) ? "Placed concept" : "Awaiting placement");
  const trace = [
    ["Source mention", jump(evidence.mention, "extraction", evidence.id)],
    ["Deduplicated concept", jump(concept.name, "deduplication", concept.id)],
    [parent ? "Candidate parent" : "Hierarchy position", jump(position, "tree", concept.id)],
  ];
  return foldedSection(id, heading,
    sentence && el("figure", { class: "source-quote" },
      el("blockquote", { lang: "en" }, excerpt(sentence, evidence.source_span || evidence.mention)),
      el("figcaption", {}, articleLink(evidence.pmid))),
    el("dl", { class: "trace-links" }, trace.map(([label, value]) => el("div", {},
      el("dt", {}, label), el("dd", {}, value)))));
}

function renderPapers(articles, pmidMentions, byMention) {
  const [id, heading] = overviewSections[3];
  return foldedSection(id, `${heading} (${articles.length})`, el("ol", { class: "research-sources" }, articles.map(article => {
    const rows = pmidMentions.get(article.pmid) || [];
    const kept = rows.filter(row => byMention.get(row.id)?.status === "keep").length;
    return el("li", {},
      el("span", { class: "paper-title", lang: "en" },
        rows.length ? jump(article.title, "extraction", rows[0].id) : article.title),
      el("span", { class: "paper-meta" }, articleLink(article.pmid),
        el("span", {}, `${rows.length} ${rows.length === 1 ? "mention" : "mentions"}, ${kept} retained`)));
  })));
}

export function renderOverview({ mount, data, stages }) {
  const { run, articles, mentions, normalization, equivalence, retrieval, placement, conceptOrder } = data;
  const byMention = new Map(normalization.map(row => [row.id, row]));
  const rawMentions = new Map(mentions.map(row => [row.id, row]));
  const nodes = new Map(placement.nodes.map(node => [node.id, node]));
  const specificEdges = placement.edges.filter(edge => edge.support !== "schema_type");
  const concepts = new Map(equivalence.concepts.map(concept => [concept.id, concept]));
  const exampleEdge = specificEdges.find(edge => concepts.has(edge.child_id));
  const concept = concepts.get(exampleEdge?.child_id) || equivalence.concepts.find(row => nodes.has(row.id)) || equivalence.concepts[0];
  const sourceMentions = concept?.source_mentions.map(id => rawMentions.get(id)).filter(Boolean) || [];
  const evidence = sourceMentions.find(row => row.offset_status === "exact" && row.context) || sourceMentions[0];
  const kept = normalization.filter(row => row.status === "keep").length;
  const points = retrieval.points || [];
  const clusters = new Set(points.filter(row => row.cluster_id >= 0).map(row => row.cluster_id));
  const totals = {
    extraction: countLabel(mentions.length, "mention"),
    normalization: `${kept} retained`,
    embedding: countLabel(points.length, "vector"),
    clustering: countLabel(clusters.size, "cluster"),
    deduplication: countLabel(concepts.size, "concept"),
    ordering: countLabel(conceptOrder.groups.length, "entity type"),
    tree: countLabel(placement.edges.length, "candidate edge"),
  };
  const metrics = [
    [jump("Entity mentions", "extraction"), mentions.length, countLabel(mentions.filter(row => row.offset_status === "exact").length, "exact source span")],
    [jump("Retained mentions", "normalization"), kept, normalization.filter(row => row.status === "review").length + " pending review"],
    [jump("Deduplicated concepts", "deduplication"), concepts.size, countLabel(retrieval.candidate_count, "candidate pair")],
    [jump("Candidate hierarchy edges", "tree"), placement.edges.length, countLabel(specificEdges.length, "specific parent–child edge")],
  ];
  const highlights = [
    ["Entity mentions", mentions.length, "extraction"],
    ["Retained mentions", kept, "normalization"],
    ["Deduplicated concepts", concepts.size, "deduplication"],
    ["Parent–child edges", specificEdges.length, "tree"],
  ];

  const pmidMentions = new Map();
  for (const mention of mentions) {
    if (!pmidMentions.has(mention.pmid)) pmidMentions.set(mention.pmid, []);
    pmidMentions.get(mention.pmid).push(mention);
  }

  const created = new Date(run.created_at || "");
  const date = Number.isNaN(created.valueOf()) ? null :
    el("time", { datetime: run.created_at }, new Intl.DateTimeFormat("en-GB", { dateStyle: "long" }).format(created));
  const metadata = [
    ["Batch ID", el("code", {}, run.run_id)],
    run.extraction_commit && ["Extractor commit", el("code", {}, run.extraction_commit.slice(0, 12))],
    run.extraction_model && ["Extraction model", el("code", {}, run.extraction_model)],
    placement.model && ["Placement model", el("code", {}, placement.model)],
    placement.entity_reference_count != null && ["Entity reference lookups", placement.entity_reference_count],
    ["GO lookups", placement.go_reference_count ?? 0],
  ].filter(Boolean);

  mount.append(section(...overviewSections[0],
      el("p", { class: "batch-intro" }, batchTitle(run)),
      el("p", { class: "batch-meta" }, date && [date, " · "], countLabel(articles.length, "PubMed article")),
      el("nav", { class: "overview-metrics", "aria-label": "Key results" },
        highlights.map(([label, value, stage]) => jump([el("strong", {}, String(value)), label], stage))),
      disclosure("Detailed counts", table("Batch results", ["Metric", "Count", "Details"], metrics, "metric-table")),
      renderTrace(concept, evidence, nodes.get(exampleEdge?.parent_id), nodes),
      foldedSection(...overviewSections[2],
        table("Workflow", ["Stage", "What to inspect", "Batch result"], stages.map(stage =>
          [jump(stage.label, stage.id), stage.detail, totals[stage.id]]), "workflow-table")),
      renderPapers(articles, pmidMentions, byMention),
      disclosure("Batch details", el("div", { class: "origin-line" },
        el("dl", {}, metadata.map(([label, value]) => el("div", {}, el("dt", {}, label), el("dd", {}, value)))))),
  ));
}

function countLabel(count, singular) {
  return count + " " + singular + (count === 1 ? "" : "s");
}
