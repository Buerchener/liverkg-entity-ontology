import { disclosure, el, status, articleLink, modelNote, typeLabel } from './ui.js';

const decisions = {
  keep: ['Retained', 'good'], review: ['Needs review', 'warn'], outside_scope: ['Outside scope', 'neutral'],
  equivalent: ['Equivalent', 'good'], distinct: ['Distinct', 'neutral'], uncertain: ['Uncertain', 'warn']
};
const upstream = {
  retained_span: 'Original span retained', linked_to_other_span: 'Linked to a candidate at another span',
  filtered: 'Filtered by the extractor', unresolved_link: 'No linked candidate', not_run: 'Verification skipped',
  ontology_coverage: 'Recovered in the coverage check'
};
const filterReasons = {
  cell_type_not_specific: 'Cell type too broad',
  bare_process_without_direct_evidence: 'Process mentioned without direct evidence',
  invalid_entity_type: 'Invalid entity type',
  organ_name_used_only_as_disease_modifier: 'Organ name used as a disease modifier',
  tissue_not_anatomical: 'Tissue label does not identify an anatomical structure',
  generic_disease_category: 'Disease category too broad',
  composite_entity: 'Composite entity',
  pathological_process_not_disease: 'Pathological process rather than disease'
};
const retrievalReasons = {
  shared_name_or_alias: 'Shared name or alias', global_knn: 'Global KNN',
  global_top_two: 'Global nearest neighbors', within_cluster: 'Within-cluster neighbors'
};
const locations = {exact:'Offset verified', missing:'Missing offsets', mismatch:'Offset mismatch'};
const co = (tag, cls, ...children) => el(tag, {class: cls}, ...children);
const small = text => co('span', 'ev-small', text);
const copy = text => co('p', 'ev-copy', text);
const note = (text, label) => text ? co('div', 'ev-copy', modelNote(text, label)) : null;
const badge = value => status(...(decisions[value] || [value, 'neutral']));
const fold = value => String(value ?? '').trim().toLowerCase();
const group = (title, ...children) => co('section', 'ev-group', el('h3', {}, title), ...children);

function aliasesLine(aliases) {
  return small(['Aliases · ', ...aliases.flatMap((alias, index) =>
    [index ? ', ' : '', modelNote(alias, 'Download original alias')])]);
}

function action(label, handler, css = 'ev-link') {
  const button = co('button', css, label);
  button.type = 'button';
  button.addEventListener('click', handler);
  return button;
}

function selectControl(title, choices) {
  const select = el('select', {'aria-label': title});
  choices.forEach(([value, label]) => {
    const option = el('option', {}, label);
    option.value = value;
    select.append(option);
  });
  return select;
}

function searchControl(placeholder) {
  const field = el('input', {type: 'search', placeholder, 'aria-label': placeholder, autocomplete: 'off'});
  return field;
}

function field(control) {
  const label = control.type === 'search' ? 'Search records' : control.getAttribute('aria-label');
  return co('label','ev-field',el('span',{},label),control);
}

function card(top, bottom, trailing) {
  const headline = co('span', 'ev-card-top', co('strong', '', top));
  if (trailing) headline.append(trailing);
  return [headline, bottom ? co('span', 'ev-card-meta', bottom) : null];
}

function articleInfo(index, pmid) {
  const article = index.articles.get(pmid);
  return co('div', 'ev-paper', articleLink(pmid), co('span', 'ev-paper-title', article?.title || ''));
}

function markedQuote(text, from, to, span, className = 'ev-quote') {
  const points = Array.from(text);
  const quote = co('blockquote',className);
  if (from >= 0 && to > from && points.slice(from,to).join('') === span) {
    quote.append(points.slice(0,from).join(''),el('mark',{},span),points.slice(to).join(''));
  } else {
    quote.textContent = text;
  }
  return quote;
}

function fullAbstract(mention, index) {
  const article = index.articles.get(mention.pmid);
  if (!article?.abstract) return null;
  const start = Array.from('TITLE: ' + article.title + '\nABSTRACT: ').length;
  return disclosure('Full abstract',
    markedQuote(article.abstract,
      mention.offset_status === 'exact' ? mention.char_start - start : -1,
      mention.char_end - start,mention.source_span,'ev-quote ev-abstract-text'));
}

function articleContext(mention, index) {
  let { context: text, context_start: start, context_end: end, context_id: where } = mention;
  // The upstream reader can split a decimal containing a space after the point.
  // Rejoin adjoining fragments only when their boundaries and source characters agree.
  const article = index.articles.get(mention.pmid);
  const source = article ? Array.from('TITLE: ' + article.title + '\nABSTRACT: ' + article.abstract) : [];
  const chunks = index.sentences.get(mention.pmid) || [];
  if (text && source.slice(start, end).join('') === text) {
    for (const c of chunks) {
      if (c.end < start && /^\s+$/.test(source.slice(c.end, start).join('')) &&
          /\d\.$/.test(c.text) && /^\d/.test(text)) {
        start = c.start;
        text = source.slice(start, end).join('');
        where = c.id + ' / ' + where;
      }
      if (c.start > end && /^\s+$/.test(source.slice(end, c.start).join('')) &&
          /\d\.$/.test(text) && /^\d/.test(c.text)) {
        end = c.end;
        text = source.slice(start, end).join('');
        where += ' / ' + c.id;
      }
    }
  }
  return {text, start, where};
}

function quoteBlock(mention, index) {
  const area = co('div', 'ev-evidence');
  if (mention.offset_status === 'exact') {
    const context = articleContext(mention, index);
    const from = mention.char_start - context.start;
    const to = mention.char_end - context.start;
    area.append(markedQuote(context.text,from,to,mention.source_span));
  } else {
    area.append(status(locations[mention.offset_status] || 'Unverified location', 'warn'),
      copy(mention.evidence_contexts.length ? 'Source details contain related sentences from this article.' :
        'Source offsets are unavailable for this mention.'));
  }
  return area;
}

function sourceDetails(mention, index, withQuote = false) {
  const sections = [articleInfo(index, mention.pmid), small('Mention ID ' + mention.id)];
  if (mention.offset_status === 'exact') {
    const {where} = articleContext(mention, index);
    sections.push(small((where === 'title' ? 'Title' : 'Abstract ' + where) +
      ' · Source offsets [' + mention.char_start + ', ' + mention.char_end + ')'));
  }
  if (withQuote) sections.push(quoteBlock(mention, index));
  if (mention.evidence_contexts.length) {
    const secondary = mention.evidence_contexts.map(evidence => [
      small((evidence.id === 'title' ? 'Title' : evidence.id) + ' · ' +
        (evidence.source === 'parenthetical_mention' ? 'Parenthetical term or abbreviation' : 'Other occurrence')),
      co('blockquote', 'ev-quote ev-alt-quote', evidence.text)
    ]);
    sections.push(disclosure('Related sentences (' + mention.evidence_contexts.length + ')', secondary));
  }
  sections.push(fullAbstract(mention, index));
  return sections;
}

function termButton(m, handler) {
  return action(m.mention + ' · ' + m.id, () => handler(m.id), 'ev-term-link');
}

function vDetails(m, index) {
  const boxes = co('div','ev-upstream');
  boxes.append(small(upstream[m.upstream_status] || m.upstream_status));
  for (const reason of m.upstream_filter_reasons) {
    boxes.append(note(filterReasons[reason] || reason.replaceAll('_', ' ')));
  }
  for (const id of m.verification_ids) {
    const v = index.verifications.get(id);
    if (v) boxes.append(copy(v.mention + ' · ' + typeLabel(v.type) + ' · ' + v.id));
  }
  return boxes;
}

function mentionInspector(m, index, stage, go) {
  const norm = index.norm.get(m.id);
  const concept = index.conceptByMention.get(m.id);
  const extraction = stage === 'extraction';
  const name = extraction ? m.mention : norm?.name || m.mention;
  const view = co('article','ev-record', el('h2', {class:'ev-title'}, name));
  const assigned = co('div', 'ev-labeled-value', status(typeLabel(m.raw_type), 'neutral'));
  if (norm?.kind && norm.kind !== m.raw_type) {
    assigned.append(co('span','ev-arrow','→'), status(typeLabel(norm.kind), 'neutral'));
  }
  if (norm) assigned.append(badge(norm.status));
  const issues = m.same_span_conflicts.flatMap(id => {
    const other = index.mentions.get(id);
    return other ? [action(other.mention + ' · ' + typeLabel(other.raw_type) + ' · ' + id,
      () => go(stage,id),'ev-term-link')] : [];
  });
  const sourceWarning = issues.length ? group('Conflicting types at the same span', issues) : null;
  const conceptLink = concept && action(concept.name, () => go('deduplication',concept.id),'ev-term-link');
  if (extraction) {
    view.append(co('div', 'ev-labeled-value ev-record-meta', status(typeLabel(m.raw_type), 'neutral'),
      m.upstream_status === 'ontology_coverage' ? status('Recovered', 'good') : null,
      articleLink(m.pmid)));
    view.append(co('section', 'ev-primary', el('h3', {}, 'Source sentence'), quoteBlock(m, index)));
    view.append(disclosure('Source details', sourceDetails(m,index)));
    view.append(disclosure('Extraction checks', vDetails(m,index), sourceWarning));
    if (norm || concept) {
      view.append(disclosure('Linked results',
        norm && group('Conceptualization',assigned,note(norm.reason),
          action('View conceptualization', () => go('normalization',m.id))),
        concept && group('Deduplicated concept',conceptLink)));
    }
    return view;
  }
  assigned.classList.add('ev-record-meta');
  assigned.append(articleLink(m.pmid));
  view.append(assigned);
  if (name !== m.mention) view.append(small('Source wording · ' + m.mention));
  if (norm) view.append(disclosure('Decision details',
    norm.initial_kind ? small('Initial conceptualization type · ' + typeLabel(norm.initial_kind)) : null,
    note(norm.definition, 'Download original definition'), note(norm.reason), note(norm.scope_note),
    norm.aliases.length ? aliasesLine(norm.aliases) : null));
  view.append(disclosure('Source evidence', sourceDetails(m,index,true),
    action('View original mention',() => go('extraction',m.id)), sourceWarning));
  if (concept) view.append(disclosure('Deduplicated concept', conceptLink));
  return view;
}

// The toolbar lives outside the changing list so typing and keyboard focus are preserved.
function renderList({root, rows, initial, controls, match, label, title, details}) {
  const list = co('ul','ev-list');
  const count = el('span',{class:'ev-count','aria-live':'polite'});
  const detail = el('div',{class:'ev-detail',tabindex:-1});
  const reset = action('Clear filters',() => {
    controls.forEach(control => control.value = '');
    draw();
    controls[0]?.focus();
  },'ev-reset');
  const [search, ...advanced] = controls;
  const filterLabel = el('span', {}, 'Filters');
  const filters = disclosure(filterLabel, co('div','ev-filter-grid',advanced.map(field)));
  const empty = co('p','ev-empty',rows.length ? 'No records match these filters.' : 'No records at this stage.');
  const left = co('section','ev-browser',
    co('div','ev-list-heading',el('h2',{},title),count),
    co('div','ev-controls',field(search),co('div','ev-filter-row',filters,reset)),list);
  root.append(co('div','ev-layout',left, detail));
  let selected = initial;
  function choose(row, jump) {
    selected = row.id;
    for (const b of list.querySelectorAll('button')) {
      b.setAttribute('aria-pressed',String(b.dataset.id === row.id));
    }
    detail.replaceChildren(details(row));
    if (jump && window.matchMedia('(max-width: 900px)').matches) {
      detail.focus({preventScroll:true});
      detail.scrollIntoView({block:'start'});
    }
  }
  function draw() {
    const visible = rows.filter(match);
    count.textContent = String(visible.length) + ' / ' + rows.length;
    const active = advanced.filter(control => control.value).length;
    filterLabel.textContent = active ? 'Filters (' + active + ' active)' : 'Filters';
    reset.hidden = controls.every(control => !control.value);
    list.replaceChildren();
    if (!visible.length) {
      list.append(el('li',{},empty));
      detail.replaceChildren(co('p','ev-detail-empty',rows.length ? 'Adjust the filters to find a record.' : 'No details for this batch.'));
      return;
    }
    for (const row of visible) {
      const b = co('button','ev-row',...label(row));
      b.type = 'button';
      b.dataset.id = row.id;
      b.addEventListener('click',() => choose(row,true));
      list.append(el('li',{},b));
    }
    choose(visible.find(r => r.id===selected) || visible[0],false);
    const button = Array.from(list.querySelectorAll('button')).find(b => b.dataset.id === selected);
    if (button && list.scrollHeight > list.clientHeight) {
      const distance = button.getBoundingClientRect().top - list.getBoundingClientRect().top;
      if (distance < 0 || distance + button.offsetHeight > list.clientHeight) list.scrollTop += distance;
    }
  }
  controls.forEach(c => c.addEventListener(c.tagName === 'INPUT' ? 'input' : 'change',draw));
  draw();
}

function mentionView(root, stage, selected, index, data, go) {
  const rows = data.mentions;
  const isNorm = stage === 'normalization';
  const search = searchControl('Name, abbreviation or PMID');
  const papers = selectControl('Article', [['','All articles'],...data.articles.map(p => [p.pmid,'PMID '+p.pmid])]);
  const counts = new Map();
  const stateFor = m => isNorm ? index.norm.get(m.id)?.status : m.offset_status;
  for (const m of rows) counts.set(stateFor(m),(counts.get(stateFor(m)) || 0)+1);
  const choices = isNorm ? ['keep','review','outside_scope'] : ['exact','missing','mismatch'];
  const textFor = v => isNorm ? decisions[v][0] : locations[v];
  const filter = selectControl(isNorm?'Decision':'Source location', [['','All statuses'],
    ...choices.map(v=>[v,textFor(v)+' · '+(counts.get(v) || 0)])]);
  const recovered = rows.filter(m=>m.upstream_status==='ontology_coverage').length;
  const origin = !isNorm && recovered ? selectControl('Mention source', [['','All sources'],
    ['native','Original extraction · '+(rows.length-recovered)],['coverage','Coverage check · '+recovered]]) : null;
  const concept = index.concepts.get(selected);
  const initial = concept?.source_mentions[0] || selected;
  const searchMatches = m => {
    const norm = index.norm.get(m.id);
    const c = index.conceptByMention.get(m.id);
    return fold([m.mention,m.raw_type,typeLabel(m.raw_type),m.id,
      norm?.name,norm?.kind,typeLabel(norm?.kind),...(norm?.aliases || []),c?.name,...(c?.aliases || [])].join(' ')).includes(fold(search.value));
  };
  renderList({root,rows,initial,controls:[search,papers,filter,...(origin?[origin]:[])],title:isNorm?'Mention decisions':'Entity mentions',
    match:m=>(!papers.value || m.pmid===papers.value) && (!filter.value || stateFor(m)===filter.value) &&
      (!origin?.value || (origin.value==='coverage') === (m.upstream_status==='ontology_coverage')) && searchMatches(m),
    label:m=>card(m.mention,typeLabel(isNorm ? index.norm.get(m.id)?.kind || m.raw_type : m.raw_type),isNorm ? badge(stateFor(m)) :
      m.upstream_status==='ontology_coverage' ? status('Recovered','good') :
        m.offset_status!=='exact' ? status(locations[m.offset_status] || 'Unverified location','warn') : null),
    details:m=>mentionInspector(m,index,stage,go)});
}

function pairNames(pair, index) {
  return pair.id.split('|').map(id => index.mentions.get(id)?.mention || id);
}

function conceptInspector(c, index, go, openPair) {
  const mentionCount = c.source_mentions.length;
  const section = co('article','ev-record',co('h2','ev-title',c.name),
    co('div', 'ev-labeled-value ev-record-meta', status(typeLabel(c.kind),'neutral'),
      small(mentionCount + (mentionCount === 1 ? ' source mention' : ' source mentions'))),
    disclosure('Concept details', small('Concept ID ' + c.id),
      note(c.definition, 'Download original definition'),
      c.aliases.length ? aliasesLine(c.aliases) : null));
  if (c.type_review_status==='needs_review') section.append(status('Entity type needs review','warn'));
  const sources = c.source_mentions.flatMap(id => {
    const m=index.mentions.get(id);
    return m ? [co('div','ev-source-item', termButton(m, sourceId=>go('extraction',sourceId)),
      articleLink(m.pmid), quoteBlock(m,index), disclosure('Source details', sourceDetails(m,index)))] : [];
  });
  if (sources.length) section.append(disclosure('Source mentions ('+mentionCount+')', sources));
  const related=index.pairs.filter(p=>p.id.split('|').some(id=>c.source_mentions.includes(id)));
  if (related.length) {
    const pairs = related.map(p => action(pairNames(p,index).join(' / ')+' · '+decisions[p.applied_verdict][0],
      ()=>openPair(p.id),'ev-term-link'));
    section.append(disclosure('Related equivalence judgments ('+related.length+')', pairs));
  }
  section.append(action('View concept in hierarchy',()=>go('tree',c.id)));
  return section;
}

function pairInspector(pair, index, go) {
  const selected = index.retrieval.get(pair.id);
  const box = co('article','ev-record',co('h2','ev-title',pairNames(pair,index).join(' / ')),
    co('div','ev-labeled-value ev-record-meta',badge(pair.applied_verdict)));
  const review = [note(pair.reason)];
  if (pair.decision_source==='type_gate') review.push(copy('Different entity types. Equivalence review was skipped.'));
  if (pair.decision_source==='type_review_pending') review.push(copy('Entity type needs review. Both mentions were kept separate.'));
  if (pair.decision_source==='single_missing_pair') review.push(copy('Follow-up review of the only unjudged pair in the group.'));
  if (pair.verdict && pair.applied_verdict!==pair.verdict) review.push(copy('Initial model judgment: '+decisions[pair.verdict][0]));
  if (selected) {
    const labels = selected.reasons.map(r=>retrievalReasons[r]||r).join(', ');
    review.push(small('Cosine similarity '+selected.cosine_similarity.toFixed(3)+' · '+labels));
  }
  if (review.some(Boolean)) box.append(disclosure('Review details', review));
  const evidence = pair.id.split('|').flatMap(id => {
    const m = index.mentions.get(id);
    const norm = index.norm.get(id);
    const check = index.typeRechecks.get(id);
    return m ? [group(m.mention,status(typeLabel(norm?.kind || m.raw_type),'neutral'),
      check ? copy('Source-based type recheck: '+typeLabel(check.initial_kind)+' → '+typeLabel(check.kind)) : null,
      check?.source_quote ? co('blockquote','ev-copy',check.source_quote) : null,
      check ? note(check.reason) : null,
      sourceDetails(m,index,true),action('View mention '+id,()=>go('extraction',id)))] : [];
  });
  if (evidence.length) box.append(disclosure('Source mentions (' + evidence.length + ')', evidence));
  return box;
}

function dedupView(root, selected, index, data, go) {
  const concepts=data.equivalence.concepts;
  const judgments=index.pairs;
  const selectedPair=judgments.find(p=>p.id===selected || p.id.split('|').reverse().join('|')===selected);
  let mode=selectedPair?'pairs':'concepts';
  const menu=co('div','ev-mode');
  const content=co('div','ev-mode-panel');
  let focused=selectedPair?.id || index.conceptByMention.get(selected)?.id || selected;
  const tabs=[['concepts','Concepts'],['pairs','Equivalence judgments']];
  function show(tab,id) {
    mode=tab;
    focused=id;
    content.replaceChildren();
    for (const btn of menu.children) btn.setAttribute('aria-pressed',String(btn.dataset.mode===mode));
    const search=searchControl(mode==='concepts'?'Concept or alias':'Mention name or PMID');
    if (mode==='concepts') {
      const kinds=[...new Set(concepts.map(c=>c.kind))].sort();
      const type=selectControl('Entity type',[['','All types'],...kinds.map(k=>[k,typeLabel(k)])]);
      renderList({root:content, rows:concepts,initial:focused,controls:[search,type],title:'Deduplicated concepts',
        match:c=>(!type.value || c.kind===type.value)&&fold([c.name,c.kind,typeLabel(c.kind),...c.aliases,...c.source_mentions].join(' ')).includes(fold(search.value)),
        label:c=>card(c.name,typeLabel(c.kind)+' · '+c.source_mentions.length+
          (c.source_mentions.length === 1 ? ' mention' : ' mentions')),
        details:c=>conceptInspector(c,index,go,id=>show('pairs',id))});
    } else {
      const verdicts=['equivalent','distinct','uncertain'];
      const filter=selectControl('Equivalence judgment',[['','All judgments'],...verdicts.map(v=>[v,decisions[v][0]+' · '+judgments.filter(p=>p.applied_verdict===v).length])]);
      renderList({root:content,rows:judgments,initial:focused,controls:[search,filter],title:'Candidate pairs',
        match:p=>(!filter.value || p.applied_verdict===filter.value) && fold([p.id,...pairNames(p,index)].join(' ')).includes(fold(search.value)),
        label:p=>card(pairNames(p,index).join(' / '),null,badge(p.applied_verdict)),
        details:p=>pairInspector(p,index,go)});
    }
  }
  for (const [key,label] of tabs) {
    const button=action(label,()=>show(key,null),'ev-mode-button');
    button.dataset.mode=key;
    menu.append(button);
  }
  root.append(menu,content);
  show(mode,focused);
}

export function renderEvidence({stage,mount,data,go,selected}) {
  mount.replaceChildren();
  const root=co('div','ev-stage');
  mount.append(root);
  const concepts=data.equivalence.concepts;
  const index={
    articles:new Map(data.articles.map(a=>[String(a.pmid),a])),
    mentions:new Map(data.mentions.map(m=>[m.id,m])),
    norm:new Map(data.normalization.map(r=>[r.id,r])),
    typeRechecks:new Map((data.equivalence.type_rechecks || []).map(r=>[r.id,r])),
    verifications:new Map(data.verification_candidates.map(r=>[r.id,r])),
    concepts:new Map(concepts.map(c=>[c.id,c])),
    conceptByMention:new Map(concepts.flatMap(c=>c.source_mentions.map(id=>[id,c]))),
    pairs:data.equivalence.pair_judgments,
    retrieval:new Map(data.retrieval.pairs.map(p=>[p.left+'|'+p.right,p])),
    sentences:new Map()
  };
  for (const [pmid] of index.articles) {
    const units=new Map();
    for (const m of data.mentions.filter(r=>r.pmid===pmid)) {
      if (m.context_id) units.set(m.context_id,{id:m.context_id,text:m.context,start:m.context_start,end:m.context_end});
      for (const c of m.evidence_contexts) units.set(c.id,c);
    }
    index.sentences.set(pmid,[...units.values()].sort((a,b)=>a.start-b.start));
  }
  if (stage==='deduplication') dedupView(root,selected,index,data,go);
  else mentionView(root,stage,selected,index,data,go);
}
