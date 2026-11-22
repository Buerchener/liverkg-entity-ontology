import { batchTitle, el } from "./ui.js";
import { overviewSections, renderOverview } from "./overview.js";

const groups = [
  ["Source Evidence", [
    { id: "extraction", label: "Entity Extraction", detail: "Select a mention to read its source sentence.", module: "evidence", method: "renderEvidence" },
    { id: "normalization", label: "Conceptualization", detail: "Select a mention to review its type and scope decision.", module: "evidence", method: "renderEvidence" },
  ]],
  ["Concept Preparation", [
    { id: "embedding", label: "Embedding", detail: "Select a point to trace it to the source.", module: "preparation", method: "renderPreparation" },
    { id: "clustering", label: "Clustering", detail: "", module: "preparation", method: "renderPreparation" },
    { id: "deduplication", label: "Deduplication", detail: "Select a concept to see which mentions were merged.", module: "evidence", method: "renderEvidence" },
  ]],
  ["Entity Hierarchy", [
    { id: "ordering", label: "Concept Ordering", detail: "Browse concepts from broad to specific within each type.", module: "preparation", method: "renderPreparation" },
    { id: "tree", label: "Hierarchy Placement", detail: "Browse the ontology by entity type or focus on an individual concept.", module: "incremental", method: "renderIncremental" },
  ]],
  ["Literature Graph", [
    { id: "links", label: "Backbone Linking", detail: "Select a concept to see the v2 backbone node it links to and why.", module: "relations", method: "renderRelations" },
    { id: "relations", label: "Concept Relations", detail: "Browse verified relations merged onto concepts, with their gate, novelty and source sentences.", module: "relations", method: "renderRelations" },
  ]],
];
const stages = groups.flatMap(([, children]) => children);
const title = document.querySelector("#heading");
const content = document.querySelector("#content");
const nav = document.querySelector("#stages");
const crumb = document.querySelector("#crumb");
const subtitle = document.querySelector("#subtitle");
const run = new URLSearchParams(location.search).get("run");
const overviewLink = el("a", { href: "#overview", class: "workbench-nav-link workbench-overview" }, "Batch Overview");
nav.append(overviewLink);
for (const [label, children] of groups) {
  nav.append(el("section", { class: "nav-group", "aria-label": label },
    el("p", { class: "nav-group-title", "aria-hidden": "true" }, label),
    el("ol", { class: "workbench-stage-list" }, children.map(stage =>
      el("li", {}, el("a", { class: "workbench-nav-link", href: "#" + stage.id,
        onClick: event => { if (!data?.run) event.preventDefault(); } },
        stage.label))))));
}

let data;
let renderVersion = 0;
let renderedStage;
const loadedStyles = new Map();

function go(stage, selected) {
  const hash = "#" + stage + (selected ? "/" + encodeURIComponent(selected) : "");
  if (location.hash !== hash) location.hash = hash;
  else render();
}

function stageStyle(module) {
  if (!loadedStyles.has(module)) {
    loadedStyles.set(module, new Promise((resolve, reject) => {
      const link = el("link", { rel: "stylesheet", href: "/" + module + ".css" });
      link.onload = resolve;
      link.onerror = () => {
        link.remove();
        loadedStyles.delete(module);
        reject(new Error("Stage stylesheet failed to load."));
      };
      document.head.append(link);
    }));
  }
  return loadedStyles.get(module);
}

function renderLocation(stage) {
  for (const link of nav.querySelectorAll("a")) {
    if (link !== overviewLink && !data?.run) {
      link.setAttribute("aria-disabled", "true");
      link.tabIndex = -1;
    } else {
      link.removeAttribute("aria-disabled");
      link.removeAttribute("tabindex");
    }
    if (link.hash === "#" + (stage?.id || "overview")) {
      link.setAttribute("aria-current", stage ? "step" : "page");
      if (nav.scrollWidth > nav.clientWidth) {
        const bounds = nav.getBoundingClientRect();
        const item = link.getBoundingClientRect();
        if (item.left < bounds.left || item.right > bounds.right) {
          nav.scrollLeft += item.left - bounds.left - 16;
        }
      }
    } else {
      link.removeAttribute("aria-current");
    }
  }
  crumb.replaceChildren(stage ? el("a", { href: "#overview" }, "Batch Overview") :
    el("span", { "aria-current": "page" }, "Batch Overview"));
  if (stage) crumb.append(el("span", { "aria-hidden": "true" }, "/"), el("span", { "aria-current": "page" }, stage.label));
  title.textContent = stage?.label || "Batch Overview";
  document.title = (stage?.label || "Entity Ontology Workbench") + " · LiverTrail";
  subtitle.textContent = stage?.detail ?? "";
  subtitle.hidden = !subtitle.textContent;
}

function adjacentStages(stage) {
  const index = stages.indexOf(stage);
  const previous = stages[index - 1];
  const next = stages[index + 1];
  return el("nav", { class: "adjacent-stages", "aria-label": "Adjacent stages" },
    el("a", { href: previous ? "#" + previous.id : "#overview" },
      el("small", {}, previous ? "Previous" : "Back"), el("strong", {}, previous?.label || "Batch Overview")),
    next && el("a", { href: "#" + next.id },
      el("small", {}, "Next"), el("strong", {}, next.label)));
}

async function render() {
  const turn = ++renderVersion;
  const [token, encoded] = location.hash.slice(1).split("/");
  const stage = data?.run && stages.find(item => item.id === token);
  let selected = null;
  if (encoded) {
    try { selected = decodeURIComponent(encoded); } catch { selected = null; }
  }
  const overviewSection = !stage && overviewSections.some(([id]) => id === selected) ? selected : null;
  renderLocation(stage);
  if (!data) return;
  const target = stage?.id || "overview";
  if (renderedStage !== target || (!overviewSection && !stage)) window.scrollTo(0, 0);
  renderedStage = target;

  const mount = el("div", { class: "stage-body", id: "stage-view" });
  if (!data.run) {
    mount.append(el("section", { class: "empty empty-state" },
      el("h2", {}, "No completed batches"),
      el("p", {}, "Run the entity ontology workflow, then refresh this page to see its results."),
      el("button", { class: "btn", type: "button", onClick: () => location.reload() }, "Refresh batches")));
    content.replaceChildren(mount);
    return;
  }
  if (!stage) {
    content.replaceChildren(mount);
    renderOverview({ mount, data, stages });
    if (overviewSection) {
      const section = document.getElementById(overviewSection);
      if (section instanceof HTMLDetailsElement) section.open = true;
      section?.scrollIntoView();
    }
    return;
  }
  try {
    const [module] = await Promise.all([import("./" + stage.module + ".js"), stageStyle(stage.module)]);
    if (turn !== renderVersion) return;
    content.replaceChildren(mount, adjacentStages(stage));
    module[stage.method]({ stage: stage.id, mount, data, placement: data.placement, go, selected });
  } catch (error) {
    if (turn === renderVersion) fail("This stage could not be displayed.", error);
  }
}

function fail(message, error) {
  console.error(error);
  content.replaceChildren(el("section", { class: "empty empty-state", role: "alert" },
    el("h2", {}, "Could not load results"), el("p", {}, message),
    el("div", { class: "controls" },
      el("button", { class: "btn", type: "button", onClick: () => location.reload() }, "Retry"),
      run && el("a", { href: "/", class: "text-link" }, "Open the latest batch"))));
}

function loadChoices() {
  if (!data.availableRuns?.length) return;
  const select = document.querySelector("#batch-choice");
  for (const row of data.availableRuns) {
    select.append(el("option", { value: row.run_id }, batchTitle(row)));
  }
  select.value = data.run.run_id;
  select.addEventListener("change", () => {
    const url = new URL(location.href);
    url.searchParams.set("run", select.value);
    url.hash = location.hash.split("/")[0];
    location.assign(url);
  });
  document.querySelector("#batch-switcher").hidden = false;
}

document.querySelector(".workbench-skip").addEventListener("click", event => {
  event.preventDefault();
  document.querySelector("#main").focus();
});
window.addEventListener("hashchange", render);
render();
try {
  const query = run === null ? "" : "?" + new URLSearchParams({ run });
  const response = await fetch("/api/batch" + query);
  if (!response.ok) throw new Error("HTTP " + response.status);
  data = await response.json();
  if (data.run) loadChoices();
  await render();
} catch (error) {
  fail(run ? "Try again or open the latest completed batch." : "Check that the workbench server is running.", error);
}
