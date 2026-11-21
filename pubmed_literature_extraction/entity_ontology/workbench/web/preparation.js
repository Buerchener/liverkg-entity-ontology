import { disclosure, el, modelNote, status, typeLabel } from './ui.js';

const MAJOR_COLORS = ['#0072B2', '#D55E00', '#009E73', '#CC79A7', '#E69F00', '#56B4E9'];
const OTHER_COLOR = '#7E909C';
const NOISE_COLOR = '#65717C';

const VERDICTS = {
  equivalent: 'Equivalent',
  distinct: 'Distinct',
  uncertain: 'Uncertain',
};
const PAIR_REASONS = { shared_name_or_alias:'Shared name or alias', global_knn:'Embedding neighbour', global_top_two:'Embedding neighbour', within_cluster:'Neighbour in the same cluster' };

function indexBy(rows, key) {
  return new Map(rows.map(row => [row[key], row]));
}

function button(label, onClick, className = 'btn') {
  return el('button', {type: 'button', class: className, onClick}, label);
}

function sectionTitle(text) {
  return el('h2', {class: 'prep-section-title'}, text);
}

function focusInspector(inspector) {
  if (!window.matchMedia('(max-width: 900px)').matches) return;
  inspector.focus({preventScroll:true});
  inspector.scrollIntoView({block:'start'});
}

function labelledSelect(label, onChange) {
  const select = el('select', {'aria-label': label});
  select.addEventListener('change', () => onChange(select.value));
  return {select, wrapper: el('label', {class: 'prep-field'}, el('span', {}, label), select)};
}

function option(value, label) {
  return el('option', {value: String(value)}, label);
}

function keyValue(rows) {
  const dl = el('dl', {class: 'prep-fields'});
  for (const [key, value] of rows) {
    dl.append(el('dt', {}, key), el('dd', {}, value));
  }
  return dl;
}

function clusterText(id) {
  return Number(id) === -1 ? 'Noise' : 'Cluster ' + id;
}

function aliasList(aliases) {
  return aliases.length ? aliases.map((item, i) =>
    [i ? ', ' : '', modelNote(item, 'Download original alias')]) : 'None';
}

function clusterStyle(points) {
  const countById = new Map();
  for (const point of points) {
    countById.set(point.cluster_id, (countById.get(point.cluster_id) || 0) + 1);
  }
  const noise = countById.get(-1) || 0;
  countById.delete(-1);
  const clusters = [...countById].sort(([idA, countA], [idB, countB]) => countB - countA || idA - idB);
  const highlighted = clusters.slice(0, MAJOR_COLORS.length);
  const colors = new Map(highlighted.map(([id], index) => [id, MAJOR_COLORS[index]]));
  const otherCount = clusters.length - highlighted.length;
  const otherMentions = clusters.slice(highlighted.length).reduce((sum, [, count]) => sum + count, 0);
  return {
    clusters, highlighted, noise, otherCount, otherMentions, otherColor: OTHER_COLOR,
    isHighlighted: id => colors.has(id),
    pointColor: id => id === -1 ? NOISE_COLOR : colors.get(id) || OTHER_COLOR,
  };
}

function tickRange(values) {
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const padding = Math.max((hi - lo) * .05, .03);
  const min = lo - padding;
  const max = hi + padding;
  const delta = (max - min) / 5;
  const power = Math.pow(10, Math.floor(Math.log10(delta)));
  const step = [1, 2, 2.5, 5, 10].map(x => x * power).find(x => x >= delta);
  const ticks = [];
  for (let tick = Math.ceil(min / step) * step; tick <= max; tick += step) ticks.push(tick);
  return {min, max, ticks};
}

function pcaAxis(name, ratio) {
  return ratio == null ? name : name + ' (' + (ratio * 100).toFixed(1) + '%)';
}

function svgNode(tag, attrs, content) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (content != null) node.textContent = content;
  return node;
}

function marker(x, y, color, noise, opacity = 1) {
  if (noise) {
    const radius = 3.5;
    return svgNode('polygon', {
      points: `${x},${y-radius} ${x+radius},${y} ${x},${y+radius} ${x-radius},${y}`,
      fill: 'none', stroke: color, 'stroke-width': '.9', opacity,
    });
  }
  return svgNode('circle', {cx: x, cy: y, r: '3.1', fill: color, opacity});
}

function downloadClusterSVG(points, ratios, theme, runId) {
  const width = 820, height = 565;
  const left = 78, right = 797, top = 23, bottom = 415;
  const {min: xmin, max: xmax, ticks: xticks} = tickRange(points.map(p => p.x));
  const {min: ymin, max: ymax, ticks: yticks} = tickRange(points.map(p => p.y));
  const px = x => left + (right - left) * (x - xmin) / (xmax - xmin);
  const py = y => bottom - (bottom - top) * (y - ymin) / (ymax - ymin);
  const svg = svgNode('svg', {width, height, viewBox: `0 0 ${width} ${height}`,
    role: 'img', 'font-family': 'Arial, sans-serif', 'font-size': '13', fill: '#202a35'});
  svg.append(svgNode('title', {}, 'PCA projection of HDBSCAN clustering'),
    svgNode('desc', {}, 'Each point is one retained mention. The six largest clusters are highlighted.'),
    svgNode('rect', {width, height, fill: 'white'}));
  const grid = svgNode('g', {stroke: '#e3e9ed', 'stroke-width': 1});
  const labels = svgNode('g', {'font-variant-numeric': 'tabular-nums'});
  for (const x of xticks) {
    const pos = px(x);
    grid.append(svgNode('line', {x1: pos, x2: pos, y1: top, y2: bottom}));
    labels.append(svgNode('text', {x: pos, y: bottom + 19, 'text-anchor': 'middle'}, Number(x.toFixed(2))));
  }
  for (const y of yticks) {
    const pos = py(y);
    grid.append(svgNode('line', {x1: left, x2: right, y1: pos, y2: pos}));
    labels.append(svgNode('text', {x: left - 9, y: pos + 4, 'text-anchor': 'end'}, Number(y.toFixed(2))));
  }
  svg.append(grid, labels);
  const drawOrder = [...points].sort((a, b) =>
    Number(theme.isHighlighted(a.cluster_id)) - Number(theme.isHighlighted(b.cluster_id)));
  for (const point of drawOrder) {
    const id = point.cluster_id;
    svg.append(marker(px(point.x), py(point.y), theme.pointColor(id), id === -1,
      theme.isHighlighted(id) ? .88 : .56));
  }
  const centerY = (top + bottom) / 2;
  svg.append(svgNode('path', {d: `M${left},${top}V${bottom}H${right}`,
    fill: 'none', stroke: '#778691', 'stroke-width': 1}));
  svg.append(svgNode('text', {x: (left + right) / 2, y: 459, 'text-anchor': 'middle'},
    pcaAxis('PC1', ratios?.[0])));
  svg.append(svgNode('text', {x: 24, y: centerY, 'text-anchor': 'middle',
    transform: `rotate(-90 24 ${centerY})`}, pcaAxis('PC2', ratios?.[1])));

  const legend = theme.highlighted.map(([id, n]) => ({
    color: theme.pointColor(id), label: `Cluster ${id} (n = ${n})`,
  }));
  if (theme.otherCount) legend.push({color: OTHER_COLOR, label: `Other clusters (n = ${theme.otherMentions})`});
  if (theme.noise) legend.push({color: NOISE_COLOR, label: `Noise (n = ${theme.noise})`, noise: true});
  legend.forEach(({color, label, noise}, index) => {
    const x = left + (index % 4) * 182, y = 490 + Math.floor(index / 4) * 23;
    svg.append(marker(x, y - 4, color, noise), svgNode('text', {x: x + 10, y}, label));
  });
  svg.append(svgNode('text', {x: left, y: 547, fill: '#4b5a67'},
    `${points.length} retained mentions · ${theme.clusters.length} HDBSCAN clusters`));

  const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(svg)],
    {type: 'image/svg+xml;charset=utf-8'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = (runId || 'entity-ontology').replace(/[^a-z0-9-]+/gi, '-') + '-clustering.svg';
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function plot(points, ratios, theme, onPick) {
  const {min: xmin, max: xmax, ticks: xticks} = tickRange(points.map(p => p.x));
  const {min: ymin, max: ymax, ticks: yticks} = tickRange(points.map(p => p.y));
  const xPercent = x => 100 * (x - xmin) / (xmax - xmin);
  const yPercent = y => 100 * (ymax - y) / (ymax - ymin);
  const grid = el('div', {class: 'prep-grid', role: 'group', 'aria-label': 'Selectable projection points'});
  const dotIndex = new Map();
  for (const x of xticks) {
    const line = el('span', {class: 'prep-gridline-x', 'aria-hidden':'true'},
      el('span', {}, String(Number(x.toFixed(2)))));
    line.style.left = xPercent(x) + '%';
    grid.append(line);
  }
  for (const y of yticks) {
    const line = el('span', {class: 'prep-gridline-y', 'aria-hidden':'true'},
      el('span', {}, String(Number(y.toFixed(2)))));
    line.style.top = yPercent(y) + '%';
    grid.append(line);
  }
  for (const point of points) {
    const label = point.concept_name + ' · ' + point.source_id;
    const dot = button(label, () => onPick(point.source_id), 'prep-dot');
    dot.replaceChildren(el('span', {class:'prep-sr-only'}, label));
    dot.title = label;
    dot.style.left = xPercent(point.x) + '%';
    dot.style.top = yPercent(point.y) + '%';
    dot.style.setProperty('--dot-color', theme ? theme.pointColor(point.cluster_id) : 'var(--accent, #076a70)');
    if (theme) {
      dot.classList.add('is-cluster-point');
      if (point.cluster_id === -1) dot.classList.add('is-noise');
      else if (theme.isHighlighted(point.cluster_id)) dot.classList.add('is-highlighted');
    }
    grid.append(dot);
    dotIndex.set(point.source_id, dot);
  }
  const wrap = el('figure', {class: 'prep-figure'},
    el('div', {class: theme ? 'prep-plot prep-plot-cluster' : 'prep-plot'},
      el('span', {class:'prep-axis-y'}, pcaAxis('PC2', ratios?.[1])),
      grid,
      el('span', {class:'prep-axis-x'}, pcaAxis('PC1', ratios?.[0]))),
    el('figcaption', {}, theme
      ? `${points.length} retained mentions · ${theme.clusters.length} clusters`
      : `${points.length} retained mentions`));
  return {wrap, dotIndex};
}

function clusterLegend(theme) {
  const entries = theme.highlighted.map(([id, count]) => [clusterText(id), count, theme.pointColor(id)]);
  if (theme.otherCount) entries.push([`${theme.otherCount} other clusters`, theme.otherMentions, theme.otherColor]);
  if (theme.noise) entries.push(['Noise', theme.noise, theme.pointColor(-1), true]);
  return el('ul', {class:'prep-cluster-legend', 'aria-label':'Cluster legend'}, entries.map(([name, count, color, noise]) => {
    const swatch = el('span', {class: 'prep-swatch' + (noise ? ' is-noise' : ''), 'aria-hidden':'true'});
    swatch.style.setProperty('--swatch-color', color);
    return el('li', {}, swatch, el('span', {}, name), el('span', {class:'prep-legend-count'}, String(count)));
  }));
}

function renderMap({stage, mount, data, go, selected}) {
  const retrieval = data.retrieval;
  const points = retrieval.points;
  if (!points.length) {
    mount.replaceChildren(el('p', {class:'empty'}, 'This batch has no embeddings to display.'));
    return;
  }
  const clusterMode = stage === 'clustering';
  const theme = clusterMode ? clusterStyle(points) : null;
  const pointById = indexBy(points, 'source_id');
  const normal = indexBy(data.normalization, 'id');
  const mentions = indexBy(data.mentions, 'id');
  const pairReview = indexBy(data.equivalence.pair_judgments, 'id');
  const concept = data.equivalence.concepts.find(c => c.id === selected);
  let current = pointById.has(selected) ? selected : concept?.source_mentions[0];
  if (!pointById.has(current)) current = retrieval.pairs[0]?.left || points[0].source_id;
  let activeCluster = 'all';
  let query = '';
  let shown = new Set();
  const memberSelect = labelledSelect('Select a mention', id => inspect(id));
  const search = el('input',{type:'search',placeholder:'Name, alias or PMID','aria-label':'Search mentions'});
  const matches = point => {
    if (activeCluster !== 'all' && String(point.cluster_id) !== activeCluster) return false;
    const normalized = normal.get(point.source_id);
    return [point.concept_name,point.source_id,mentions.get(point.source_id)?.mention,
      ...(normalized?.aliases || [])].join(' ').toLowerCase().includes(query);
  };
  const inspector = el('aside', {class: 'panel prep-inspector', 'aria-label':'Mention details', tabindex:-1});
  const ratios = retrieval.projection.explained_variance_ratio;
  const chart = plot(points, ratios, theme, inspect);

  function inspect(id) {
    select(id);
    focusInspector(inspector);
  }

  function refreshMembers() {
    const options = points.filter(matches);
    shown = new Set(options.map(p => p.source_id));
    memberSelect.select.replaceChildren(...options.map(p => option(p.source_id, p.concept_name + ' · ' + p.source_id)));
    if (!shown.has(current)) current = options[0]?.source_id;
    memberSelect.select.disabled = !options.length;
    if (!options.length) memberSelect.select.append(option('', 'No matching mentions'));
    select(current);
  }

  function select(id) {
    current = id;
    memberSelect.select.value = id ?? '';
    for (const [pointId, dot] of chart.dotIndex) {
      const inactive = !shown.has(pointId);
      const selected = pointId === id;
      dot.classList.toggle('is-muted', inactive);
      dot.disabled = inactive;
      dot.setAttribute('aria-pressed', String(selected));
      dot.tabIndex = selected && !inactive ? 0 : -1;
    }
    const p = pointById.get(id);
    if (!p) {
      inspector.replaceChildren(el('p',{class:'prep-inspector-empty'},'Change the filters to find a mention.'));
      return;
    }
    const normalized = normal.get(id);
    const mention = mentions.get(id);
    const pairs = retrieval.pairs.filter(pair => pair.left === id || pair.right === id)
      .sort((a,b) => b.cosine_similarity - a.cosine_similarity);
    const related = data.equivalence.concepts.find(c => c.source_mentions.includes(id));
    const list = el('ul', {class:'prep-pairs'});
    for (const pair of pairs) {
      const other = pointById.get(pair.left === id ? pair.right : pair.left);
      const pairId = pair.left + '|' + pair.right;
      const verdict = pairReview.get(pairId)?.applied_verdict;
      list.append(el('li', {}, button(other.concept_name, () => go('deduplication', pairId), 'prep-link'),
        el('span', {class:'prep-sim'}, pair.cosine_similarity.toFixed(3)),
        ...(verdict ? [status(VERDICTS[verdict] ?? verdict, verdict === 'equivalent' ? 'good' : verdict === 'uncertain' ? 'warn' : 'neutral')] : [])));
      const reasons = [...new Set(pair.reasons.map(reason => PAIR_REASONS[reason] || reason))];
      if (reasons.length) list.lastElementChild.append(el('small',{class:'prep-pair-sources'},reasons.join(', ')));
    }
    inspector.replaceChildren(
      el('h3', {}, p.concept_name),
      status(typeLabel(normalized.kind)),
      el('div', {class:'prep-actions'}, button('View source sentence', () => go('extraction', id))),
      disclosure('Mention details',
        el('p', {class:'prep-definition'}, modelNote(p.description, 'Download original definition')),
        keyValue([
          ['Mention ID', id],
          ['Location', (mention?.context_id === 'title' ? 'Title' : 'Abstract') + ' · PMID ' + id.split(':')[0]],
          ['Source wording', mention.mention],
          ['Aliases', aliasList(normalized.aliases)],
          ...(clusterMode ? [['Cluster assignment', clusterText(p.cluster_id)],
            ['Membership probability', p.cluster_id < 0 ? 'Unassigned' : (p.probability * 100).toFixed(1) + '%']] : []),
          ['Projection coordinates', '(' + p.x.toFixed(3) + ', ' + p.y.toFixed(3) + ')'],
        ]),
        related && button('View deduplicated concept', () => go('deduplication', related.id))),
      disclosure(`Candidate equivalence pairs (${pairs.length})`,
        pairs.length ? list : el('p', {class:'muted'}, 'No candidate pairs were found for this mention.'))
    );
  }

  let clusterControls;
  if (theme) {
    const filter = labelledSelect('Filter by cluster', id => {
      activeCluster = id;
      refreshMembers();
    });
    filter.select.append(option('all', `All mentions (${points.length})`));
    if (theme.noise) filter.select.append(option(-1, `Noise (${theme.noise})`));
    for (const [id, count] of theme.clusters) filter.select.append(option(id, `Cluster ${id} (${count})`));
    clusterControls = el('div', {class:'prep-cluster-controls'}, filter.wrapper,
      button('Download full SVG', () => downloadClusterSVG(points, ratios, theme, data.run?.run_id)));
  }
  search.addEventListener('input',() => {
    query = search.value.trim().toLowerCase();
    refreshMembers();
  });
  refreshMembers();
  const projectionTitle = clusterMode ? 'HDBSCAN cluster projection' : 'Embedding projection';
  const panel = el('section', {class:'panel prep-map-panel', 'aria-label':projectionTitle},
    clusterControls, chart.wrap, theme ? clusterLegend(theme) : null,
    disclosure('Find a mention', el('div',{class:'prep-point-controls'},
      el('label',{class:'prep-field'},el('span',{},'Search mentions'),search),memberSelect.wrapper)),
    disclosure('Projection details',
      el('p', {}, theme
        ? 'HDBSCAN assigns the clusters in embedding space. The chart shows the first two principal components.'
        : 'Each embedding encodes a mention’s name, type, definition and aliases. The chart shows the first two principal components.'),
      keyValue([
        ['Embedding model', retrieval.embedding.model],
        ['Dimensions', retrieval.embedding.dimensions],
        ['Projection method', retrieval.projection.method],
      ])));
  mount.replaceChildren(el('div', {class: 'entity-prep'},
    el('div', {class: theme ? 'prep-columns prep-cluster-layout' : 'prep-columns'}, panel, inspector)));
}

function renderOrder({mount, data, go, selected}) {
  const order = data.conceptOrder;
  if (order?.status !== 'complete') {
    const messages = {
      invalid: 'The ordering results could not be loaded.',
      stale: 'Rerun concept ordering to update it for the current deduplicated concepts.',
    };
    mount.replaceChildren(el('section', {class:'panel prep-order-panel'}, sectionTitle('Concept Order'),
      el('p', {class:'muted', role:'status'}, messages[order?.status] || 'This batch has no ordering results.'),
      button('View deduplicated concepts', () => go('deduplication'))));
    return;
  }

  const concepts = data.equivalence.concepts;
  const byId = indexBy(concepts, 'id');
  const mentions = indexBy(data.mentions, 'id');
  const rows = order.groups.flatMap(group => group.concept_ids.map((id, i) => (
    {concept: byId.get(id), rank: i + 1}
  )));
  const directConcept = byId.get(selected) || concepts.find(c => c.source_mentions.includes(selected));
  let current = directConcept?.id;
  let kind = 'all';
  let query = '';
  const body = el('tbody');
  const results = el('span', {class:'prep-counter', 'aria-live':'polite'});
  const inspector = el('aside', {class:'panel prep-inspector', 'aria-label':'Concept details', tabindex:-1});
  const controls = labelledSelect('Entity type', value => {
    kind = value;
    update();
  });
  controls.select.append(option('all','All types'), ...order.groups.map(group => option(group.kind, typeLabel(group.kind))));
  const search = el('input', {type:'search', placeholder:'Name, alias or ID', 'aria-label':'Search concepts'});
  search.addEventListener('input', () => {
    query = search.value.trim().toLowerCase();
    update();
  });

  function show({concept, rank}) {
    const {id, name, kind, definition, aliases, source_mentions: sources} = concept;
    current = id;
    body.querySelectorAll('[data-node]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.node === id)));
    const links = sources.map(sourceId => {
      const source = mentions.get(sourceId);
      const label = source ? source.mention + ' · ' + sourceId : sourceId;
      return button(label, () => go('extraction', sourceId), 'prep-link');
    });
    inspector.replaceChildren(
      el('h3', {}, name),
      el('div', {class:'prep-concept-meta'}, status(typeLabel(kind)), el('span', {class:'muted'}, 'Rank ' + rank)),
      el('div', {class:'prep-actions'}, button('View hierarchy position', () => go('tree', id))),
      disclosure('Concept details',
        el('p', {class:'prep-definition'}, modelNote(definition, 'Download original definition')),
        keyValue([['Concept ID', id], ['Aliases', aliasList(aliases)]]),
        button('View deduplicated concept', () => go('deduplication', id))),
      sources.length ? disclosure(`Source mentions (${sources.length})`,
        el('div', {class:'prep-origin-mentions'}, ...links)) : null
    );
  }
  function update() {
    const filtered = rows.filter(({concept}) => (kind === 'all' || concept.kind === kind) &&
      [concept.name, concept.kind, typeLabel(concept.kind), concept.id, ...concept.aliases].some(s => s.toLowerCase().includes(query)));
    results.textContent = filtered.length + ' of ' + rows.length + ' concepts';
    body.replaceChildren();
    for (const row of filtered) {
      const b = button(row.concept.name, () => {
        show(row);
        focusInspector(inspector);
      }, 'prep-node-button');
      b.dataset.node = row.concept.id;
      body.append(el('tr', {}, el('td', {class:'prep-rank'}, String(row.rank)),
        el('td', {}, b), el('td', {}, typeLabel(row.concept.kind))));
    }
    if (!filtered.length) {
      const cell = el('td', {colspan:'3', class:'empty'}, 'No matching concepts.');
      body.append(el('tr', {}, cell));
      inspector.replaceChildren(el('p', {class:'prep-inspector-empty'}, 'Change the filters to find a concept.'));
      return;
    }
    show(filtered.find(row => row.concept.id === current) || filtered[0]);
  }
  const head = el('thead', {}, el('tr', {}, ...['Rank','Concept','Entity type'].map(t => el('th', {scope:'col'}, t))));
  const table = el('table', {class:'data-table prep-table', 'aria-label':'Concept order within each entity type'}, head, body);
  mount.replaceChildren(el('div', {class:'entity-prep'},
    el('div', {class:'prep-columns'},
      el('section', {class:'panel prep-order-panel', 'aria-label':'Ranked concepts'},
        el('div', {class:'prep-order-controls'}, controls.wrapper,
          el('label', {class:'prep-field'}, el('span', {}, 'Search concepts'), search)),
        results, el('div', {class:'prep-table-scroll'}, table),
        disclosure('How ordering works',
          el('p', {}, `The LLM orders concepts from broad to narrow within each of the ${order.groups.length} entity types. Placement follows that order.`))),
      inspector)));
  update();
}

export function renderPreparation(context) {
  if (context.stage === 'ordering') renderOrder(context);
  else renderMap(context);
}
