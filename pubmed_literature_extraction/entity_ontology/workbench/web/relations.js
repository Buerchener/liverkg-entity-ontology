import { el, status, articleLink, typeLabel } from "./ui.js";

const GATES = { AUTO: ["AUTO", "good"], REVIEW: ["REVIEW", "warn"], BLOCKED: ["BLOCKED", "bad"] };
const NOVELTY = {
  KNOWN_IN_BACKBONE: ["Known in backbone", "neutral"],
  PAIR_KNOWN_OTHER_RELATION: ["Known pair, different relation", "neutral"],
  NEW: ["New to backbone", "good"],
  UNLINKED: ["Endpoint not linked", "neutral"],
  KNOWN_IN_V2: ["Known in v2", "neutral"],
};
const LINKS = { LINKED: ["Linked", "good"], AMBIGUOUS: ["Ambiguous", "warn"], NEW: ["No backbone match", "neutral"] };
const WRITE = { IMPORT_READY: ["Import-ready", "good"], SEMANTIC_ONLY: ["Semantic only", "neutral"], HUMAN_REVIEW: ["Human review", "warn"], BLOCKED: ["Blocked", "bad"] };
const REASONS = {
  no_import_ready_evidence: "No import-ready evidence",
  counter_evidence: "Negated, contradicted or statistically unsupported evidence",
  signature_mismatch: "Endpoint types do not fit the predicate signature",
  unknown_predicate: "Predicate outside the schema",
  subject_type_mismatch: "Subject type differs from the concept type",
  object_type_mismatch: "Object type differs from the concept type",
};
const BASIS = { name: "Name or alias", normalized_id: "Extractor identifier", normalized_id_conflicts_with_name: "Identifier and name point to different nodes" };

const side = word => word === "subject" ? "Subject" : "Object";
const plural = (count, singular, pluralForm = singular + "s") => count + " " + (count === 1 ? singular : pluralForm);
const pill = (table, key) => {
  const [label, tone] = table[key] || [key || "—", "neutral"];
  return status(label, tone);
};

function reasonLabel(code) {
  if (REASONS[code]) return REASONS[code];
  const placement = code.match(/^(subject|object)_concept_(.+)$/);
  if (placement) return side(placement[1]) + " concept not yet placed in the hierarchy (" + placement[2] + ")";
  const endpoint = code.match(/^(subject|object)_(unresolved|ambiguous)$/);
  if (endpoint) return side(endpoint[1]) + (endpoint[2] === "unresolved" ? " could not be resolved to a concept" : " resolves to several concepts");
  return code;
}

function highlight(text, terms) {
  const words = [...new Set(terms.filter(Boolean))]
    .sort((a, b) => b.length - a.length)
    .map(term => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!text || !words.length) return [text || ""];
  return text.split(new RegExp("(" + words.join("|") + ")", "gi"))
    .map((part, index) => index % 2 ? el("mark", {}, part) : part);
}

function select(label, choices, value) {
  const control = el("select", { "aria-label": label });
  for (const [key, text] of choices) control.append(el("option", { value: key }, text));
  control.value = value;
  return el("label", { class: "rel-field" }, el("span", {}, label), control);
}

function search(placeholder) {
  return el("label", { class: "rel-field" }, el("span", {}, "Search"),
    el("input", { type: "search", placeholder, "aria-label": placeholder, autocomplete: "off" }));
}

function evidenceQuote(row, articles) {
  const article = articles.get(row.pmid);
  return el("figure", { class: "source-quote" },
    el("blockquote", {}, highlight(row.evidence, [row.subject, row.object])),
    el("figcaption", { class: "rel-caption" },
      articleLink(row.pmid), pill(WRITE, row.write_status),
      row.contrast_status && status("Contrast: " + row.contrast_status, "warn"),
      row.negated && status("Negated", "bad"),
      el("span", { class: "muted" }, article?.title || "")));
}

function relationCard(relation, articles, go) {
  const concept = (id, name, kind) => el("a", { href: "#links/" + encodeURIComponent(id),
    onClick: event => { event.preventDefault(); go("links", id); } }, name);
  return el("article", { class: "rel-card" },
    el("header", { class: "rel-head" },
      el("strong", {}, concept(relation.subject_concept_id, relation.subject_name)),
      el("span", { class: "muted" }, typeLabel(relation.subject_kind)),
      el("code", { class: "rel-predicate" }, relation.predicate),
      el("strong", {}, concept(relation.object_concept_id, relation.object_name)),
      el("span", { class: "muted" }, typeLabel(relation.object_kind)),
      el("span", { class: "rel-pills" }, pill(GATES, relation.gate), pill(NOVELTY, relation.novelty),
        status(plural(relation.evidence_count, "evidence item") + " · " + plural(relation.pmids.length, "article")))),
    relation.gate_reasons.length > 0 && el("p", { class: "muted rel-reasons" },
      "Reasons: " + relation.gate_reasons.map(reasonLabel).join("; ")),
    (relation.backbone_relations || relation.v2_relations || []).length > 0 && el("p", { class: "muted" },
      "Backbone relations between the endpoints: " + (relation.backbone_relations || relation.v2_relations).map(row => row.type + " (" + row.source + ")").join(", ")),
    relation.evidence.map(row => evidenceQuote(row, articles)));
}

function renderRelationList({ mount, data, go, selected }) {
  const projection = data.conceptRelations;
  const articles = new Map(data.articles.map(article => [article.pmid, article]));
  const counts = projection.counts;
  const gate = select("Gate", [["", "All"], ["AUTO", "AUTO"], ["REVIEW", "REVIEW"], ["BLOCKED", "BLOCKED"]], "");
  const novelty = select("Backbone novelty", [["", "All"],
    ...Object.entries(NOVELTY).map(([key, [label]]) => [key, label])], "");
  const query = search("Concept name or predicate");
  const list = el("div", { class: "rel-list" });
  const focus = selected && data.conceptLinks.links.find(row => row.concept_id === selected);

  function update() {
    const wanted = query.querySelector("input").value.trim().toLowerCase();
    const rows = projection.relations.filter(relation =>
      (!focus || relation.subject_concept_id === selected || relation.object_concept_id === selected) &&
      (!gate.querySelector("select").value || relation.gate === gate.querySelector("select").value) &&
      (!novelty.querySelector("select").value || relation.novelty === novelty.querySelector("select").value) &&
      (!wanted || [relation.subject_name, relation.object_name, relation.predicate]
        .some(text => text.toLowerCase().includes(wanted))));
    list.replaceChildren(...(rows.length ? rows.map(relation => relationCard(relation, articles, go))
      : [el("p", { class: "empty" }, "No relations match these filters.")]));
  }
  for (const control of [gate, novelty, query]) control.addEventListener("input", update);

  mount.append(...[
    el("p", { class: "rel-summary" },
      `${plural(counts.article_relations, "article-level relation")} were merged into ` +
      `${plural(counts.concept_relations, "concept-level relation")} (${plural(counts.merged_evidence, "duplicate evidence item")} merged). ` +
      `AUTO ${counts.gate.AUTO}, REVIEW ${counts.gate.REVIEW}, BLOCKED ${counts.gate.BLOCKED}.`),
    focus && el("p", { class: "rel-focus" }, "Showing the relations of “" + focus.name + "” · ",
      el("a", { href: "#relations" }, "Show all")),
    el("div", { class: "controls rel-controls" }, gate, novelty, query),
    list,
  ].filter(Boolean));
  update();
  if (projection.unresolved_evidence.length) {
    mount.append(el("details", { class: "rel-unresolved" },
      el("summary", {}, `Evidence whose endpoints could not be resolved to concepts (${projection.unresolved_evidence.length})`),
      projection.unresolved_evidence.map(row => el("div", { class: "rel-card" },
        el("p", { class: "rel-head" }, el("strong", {}, row.subject), el("code", { class: "rel-predicate" }, row.predicate),
          el("strong", {}, row.object)),
        el("p", { class: "muted" }, "Reasons: " + row.reasons.map(reasonLabel).join("; ")),
        evidenceQuote(row, articles)))));
  }
}

function renderLinkTable({ mount, data, go, selected }) {
  const links = data.conceptLinks;
  const counts = links.counts;
  const filter = select("Link status", [["", "All"], ["LINKED", "Linked"], ["AMBIGUOUS", "Ambiguous"], ["NEW", "No backbone match"]], "");
  const query = search("Concept name");
  const body = el("tbody");
  const relationCount = new Map();
  for (const relation of data.conceptRelations.relations) {
    for (const id of [relation.subject_concept_id, relation.object_concept_id])
      relationCount.set(id, (relationCount.get(id) || 0) + 1);
  }

  function target(row) {
    if (row.target) return el("span", {}, el("code", {}, row.target.id), " ", row.target.name);
    if (row.candidates.length) return el("span", { class: "muted" },
      row.candidates.map(item => item.id + " " + item.name).join("; "));
    return el("span", { class: "muted" }, "—");
  }

  function update() {
    const wanted = query.querySelector("input").value.trim().toLowerCase();
    const wantedStatus = filter.querySelector("select").value;
    const rows = links.links.filter(row => (!wantedStatus || row.status === wantedStatus) &&
      (!wanted || row.name.toLowerCase().includes(wanted)));
    body.replaceChildren(...rows.map(row => el("tr", { class: row.concept_id === selected ? "rel-selected" : null },
      el("td", {}, el("strong", {}, row.name), el("div", { class: "muted" }, typeLabel(row.kind))),
      el("td", {}, pill(LINKS, row.status)),
      el("td", {}, target(row)),
      el("td", { class: "muted" }, row.matched_by.map(item => BASIS[item] || item).join(", ") || "—"),
      el("td", {}, row.external_ids?.GO ? el("code", {}, row.external_ids.GO) : el("span", { class: "muted" }, "—")),
      el("td", {}, relationCount.get(row.concept_id)
        ? el("a", { href: "#relations/" + encodeURIComponent(row.concept_id),
          onClick: event => { event.preventDefault(); go("relations", row.concept_id); } },
          plural(relationCount.get(row.concept_id), "relation"))
        : el("span", { class: "muted" }, "0")))));
  }
  for (const control of [filter, query]) control.addEventListener("input", update);
  mount.append(
    el("p", { class: "rel-summary" },
      `Linked ${counts.LINKED}, ambiguous ${counts.AMBIGUOUS}, no backbone match ${counts.NEW}. ` +
      "Matches come from the offline backbone snapshot used for this batch."),
    el("div", { class: "controls rel-controls" }, filter, query),
    el("div", { class: "document-table-wrap" }, el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, ["Concept", "Link status", "Backbone node", "Basis", "GO", "Relations"].map(text => el("th", {}, text)))),
      body)));
  update();
}

export function renderRelations({ stage, mount, data, go, selected }) {
  if (!data.conceptRelations || !data.conceptLinks) {
    mount.append(el("section", { class: "empty empty-state" },
      el("h2", {}, "No relation results for this batch"),
      el("p", {}, "This batch predates relation projection. Run it again with the current version to see concept links and concept-level relations.")));
    return;
  }
  if (stage === "links") renderLinkTable({ mount, data, go, selected });
  else renderRelationList({ mount, data, go, selected });
}
