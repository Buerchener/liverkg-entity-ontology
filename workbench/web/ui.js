export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, val] of Object.entries(attrs || {})) {
    if (val == null || val === false) continue;
    if (key === "class" || key === "className") node.className = val;
    else if (key === "text" || key === "textContent") node.textContent = val;
    else if (/^on[A-Z]/.test(key)) node.addEventListener(key.slice(2).toLowerCase(), val);
    else if (key === "dataset") Object.assign(node.dataset, val);
    else if (key === "style" && typeof val === "object") Object.assign(node.style, val);
    else if (key === "htmlFor") node.htmlFor = val;
    else node.setAttribute(key, val === true ? "" : String(val));
  }
  for (const child of children.flat(Infinity))
    if (child != null && typeof child !== "boolean") node.append(child);
  return node;
}
export const status = (label, tone = "neutral") => el("span", {class: "pill tone-" + tone},label);
export const articleLink = (pmid) => el("a", {class: "article-link",href:"https://pubmed.ncbi.nlm.nih.gov/"+encodeURIComponent(pmid)+"/",target:"_blank",rel:"noreferrer"},"PMID "+pmid);

export function disclosure(label, ...children) {
  return el("details", {class: "stage-disclosure"},
    el("summary", {}, label), el("div", {class: "stage-disclosure-body"}, children));
}

const entityTypes = {
  Disease: "Disease", CellType: "Cell type", Tissue: "Tissue", Metabolite: "Metabolite",
  Protein: "Protein", Pathway: "Pathway", Gene: "Gene", MicrobialCommunity: "Microbial community",
  OrganismTaxon: "Organism or taxon", DrugTherapy: "Drug or therapy", OtherEntity: "Other entity",
  Measurement: "Measurement", Phenotype: "Phenotype", NonEntity: "Non-entity", Unclear: "Unclear",
};
export const typeLabel = kind => entityTypes[kind] || kind || "Unclear";

const han = /\p{Script=Han}/u;
const batchTitles = {
  "entity-100": "100 PubMed articles on liver disease",
  "entity-100-reference-v2": "100 PubMed articles with entity reference review",
  "entity-reference-smoke": "Entity reference pilot (5 articles)",
};

export function batchTitle(run) {
  if (run.title && !han.test(run.title)) return run.title;
  return batchTitles[run.run_id] || run.run_id;
}

export function modelNote(text, label = "Download original note") {
  if (typeof text !== "string" || !han.test(text)) return text;
  return el("button", {type: "button", class: "model-note-link", onClick: () => {
    const url = URL.createObjectURL(new Blob([text], {type: "text/plain;charset=utf-8"}));
    const link = el("a", {href: url, download: "liverkg-model-output.txt"});
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }}, label);
}
