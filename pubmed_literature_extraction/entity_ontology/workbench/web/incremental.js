import { disclosure, el, modelNote, status, typeLabel } from './ui.js';
import { layoutDag, layoutDagNeighborhood, layoutTaxonomyMap } from './dag-layout.js';

const actions = {
  new_under_root:['Under type root','neutral'], as_child:['Placed as child','good'],
  as_parent:['Placed as parent','good'], insert_between:['Inserted between','good'],
  needs_review:['Pending review','warn'], equivalent_pending:['Possible duplicate','warn'], rejected:['Placement rejected','bad'],
};
const supportLabels = {
  schema_type:['Type root','neutral'], dual_model_review:['Model review','good'], go_informed_review:['GO-assisted review','good'],
  reference_informed_review:['Reference-assisted review','good'],
};
const relations = {
  narrower:'Narrower', broader:'Broader', equivalent:'Equivalent', unrelated:'No class inclusion',
  uncertain:'Pending review', needs_review:'Pending review', rejected:'Rejected in review',
};
const referenceStatuses = {
  MATCHED:['Matched','good'], REFERENCE_ONLY:['Reference only','neutral'], NO_MATCH:['No match','neutral'],
  UNCERTAIN:['Insufficient evidence','warn'], LOOKUP_FAILED:['Lookup failed','bad'],
};
const referenceReviewStatuses = {
  reviewed:'Reviewed', no_usable_reference:'No usable reference', invalid_review:'Invalid review response',
  review_failed:'Review failed', identity_only:'Identity check',
};
const referenceReasons = {
  term_candidates_for_review:'Source terms identified for review.',
  no_exact_label_or_synonym:'The lookup found no exact label or synonym.',
  incomplete_lookup:'The source lookup was incomplete.',
  no_unique_human_gene_reference:'A unique human gene reference could not be established.',
  unsupported_entity_type:'The selected source does not cover this entity type.',
};
const reviewLabels = {true:'Supported', false:'Rejected', null:'Insufficient evidence'};

const button = (label,onClick,cls='btn') => el('button',{class:cls,type:'button',onClick},label);
const chip = (labels,key) => status(...(labels[key] || [key,'neutral']));
const countLabel = (count, singular, plural) => `${count} ${count === 1 ? singular : plural}`;
const evidenceHosts = new Set(['www.ebi.ac.uk','www.genenames.org','hgnc.genenames.org','rest.genenames.org','purl.obolibrary.org']);

function showReview(review, reference = false) {
  const outcome = reviewLabels[String(review.supported)];
  const heading = reference ? ['Reference review', outcome].filter(Boolean).join(' · ') : outcome || 'Model review';
  return el('p',{},el('strong',{},heading),
    review.reason && [' ',modelNote(review.reason)]);
}

function evidenceLink(address, label) {
  if (!address || !label) return null;
  try {
    const url = new URL(address);
    if (url.protocol === 'https:' && evidenceHosts.has(url.hostname) && !url.username && !url.password) {
      return el('a',{href:url.href,target:'_blank',rel:'noreferrer'},label);
    }
  } catch {
    return null;
  }
  return null;
}

function showReferenceStatistics(placement) {
  const conceptsBySource = placement.reference_counts_by_source;
  if (!conceptsBySource) return null;

  const lookupActions = new Set(['search','inspect','verify']);
  const countedStatuses = new Set(['observed','lookup_failed']);
  const attemptsBySource = new Map();
  for (const decision of placement.decisions) {
    for (const {action,status,source} of decision.entity_reference?.result?.query_records || []) {
      if (typeof source === 'string' && source && lookupActions.has(action) && countedStatuses.has(status)) {
        attemptsBySource.set(source,(attemptsBySource.get(source) || 0) + 1);
      }
    }
  }
  const sources = [...new Set([...Object.keys(conceptsBySource),...attemptsBySource.keys()])].sort();
  if (!sources.length) return null;

  const rows = sources.map(source => el('tr',{},
    el('th',{scope:'row'},source),
    el('td',{},String(conceptsBySource[source] ?? 0)),
    el('td',{},String(attemptsBySource.get(source) || 0))));
  return el('section',{class:'hw-ols-stats'},
    el('h4',{},'OLS source breakdown'),
    el('table',{class:'data-table','aria-label':'OLS source breakdown'},
      el('thead',{},el('tr',{},['Source','Concepts checked','Lookup attempts'].map(label=>el('th',{scope:'col'},label)))),
      el('tbody',{},rows)),
    el('p',{class:'hw-note'},'A concept can appear under several sources. Lookup attempts count searches, inspections and verifications.'));
}

export function renderIncremental({mount,placement,data,go,selected}) {
  const {nodes,edges,roots,decisions,source_concepts:source} = placement;
  const placed = new Map(nodes.map(node => [node.id,node]));
  const all = new Map([...source,...nodes].map(node => [node.id,node]));
  const mentions = new Map(data.mentions.map(mention => [mention.id,mention]));
  const outcomes = new Map(decisions.map((decision,i) => [decision.concept_id,{decision,step:i+1}]));
  const parents = new Map(nodes.map(node => [node.id,[]]));
  const children = new Map(nodes.map(node => [node.id,[]]));
  for (const edge of edges) {
    parents.get(edge.child_id).push(edge);
    children.get(edge.parent_id).push(edge);
  }
  const unplaced = source.filter(node => !placed.has(node.id));
  const specific = edges.filter(edge => edge.support !== 'schema_type').length;
  const multiParents = nodes.filter(node => parents.get(node.id).length > 1).length;
  let mode = 'overview';
  let current = all.has(selected) ? selected : undefined;
  let query = '';
  let kind = '';
  let dagMode = 'focus';
  let dagFocus = null;
  let showTypeMap = false;
  let dagPickTimer;
  const dagHistory = [];
  const isHierarchy = () => ['overview','tree','dag','diagram'].includes(mode);
  const opened = new Set();
  const typeOnlyOpened = new Set();
  const dagExpanded = new Set();
  const typeMaps = new Map();
  const tabs = new Map();
  const search = el('input',{type:'search',class:'hw-search','aria-label':'Search concepts',placeholder:'Name, alias or type'});
  const kindControl = el('select',{class:'hw-kind','aria-label':'Entity type'},
    el('option',{value:''},'All types'),
    Object.keys(roots).map(key=>el('option',{value:key},typeLabel(key))));
  const main = el('section',{class:'hw-main','aria-label':'Hierarchy browser'});
  const context = el('div',{class:'hw-context-region',hidden:true});
  const detail = el('aside',{class:'hw-detail','aria-label':'Placement details',tabindex:-1});
  const metrics = [
    ['Placed concepts', source.length-unplaced.length],
    ['Parent–child edges', specific],
    ['Unplaced concepts', unplaced.length],
  ];
  const supplementaryMetrics = [['Concepts with multiple parents', multiParents]];
  if (placement.entity_reference_count != null) supplementaryMetrics.push(['Concepts sent for reference lookup',placement.entity_reference_count]);
  else if (placement.go_reference_count != null) supplementaryMetrics.push(['Concepts with GO reference',placement.go_reference_count]);
  const moreMetrics = el('dl',{class:'hw-metrics hw-metrics-extra'},
    supplementaryMetrics.map(([label,value])=>el('div',{},el('dt',{},label),el('dd',{},String(value)))));
  const toolbar = el('div',{class:'hw-toolbar'});
  const tabList = el('div',{class:'hw-tabs'});
  const extraTabs = el('div',{class:'hw-extra-views'});
  const extraLabel = el('span',{},'More views');
  const moreViews = disclosure(extraLabel,extraTabs);
  moreViews.classList.add('hw-more-views');
  const primaryViews = ['overview','tree','dag'];
  const views = [['overview','Overview'],['tree','Tree'],['dag','DAG'],['diagram','Tree diagram'],['steps',`Placement Log (${decisions.length})`],['unplaced',`Unplaced (${unplaced.length})`]];
  for (const [id,label] of views) {
    const control = button(label,()=> {
      if (mode===id) return;
      mode=id;
      if (mode==='overview') {
        showOverview();
        return;
      }
      if (isHierarchy() && placed.has(current)) rememberPath(current);
      refresh();
      revealCurrent();
    },'hw-tab');
    tabs.set(id,control);
    (primaryViews.includes(id) ? tabList : extraTabs).append(control);
  }
  function labelMoreViews() {
    const alternate = !primaryViews.includes(mode);
    extraLabel.textContent = alternate && !moreViews.open ? tabs.get(mode).textContent : 'More views';
  }
  moreViews.addEventListener('toggle',labelMoreViews);
  toolbar.append(tabList,moreViews);
  search.addEventListener('input',()=> { query=search.value.trim().toLowerCase(); renderMain(); });
  kindControl.addEventListener('change',()=> {
    kind=kindControl.value;
    if (kind && dagFocus && all.get(dagFocus).kind!==kind) {
      dagFocus=null;
      dagHistory.length=0;
    }
    if (kind && all.get(current)?.kind!==kind) {
      current = isHierarchy() && !query ? (mode==='overview' ? undefined : roots[kind]) :
        (mode==='unplaced' ? unplaced : source).find(matches)?.id;
    }
    if (mode==='tree' && kind) opened.add(roots[kind]);
    refresh();
  });
  const layout = el('div',{class:'hw-layout'},
      el('section',{class:'hw-browser'},toolbar,el('div',{class:'hw-filters'},
        el('label',{class:'hw-field'},el('span',{},'Search concepts'),search),
        el('label',{class:'hw-field'},el('span',{},'Entity type'),kindControl)),
      context,main),detail);
  mount.replaceChildren(el('div',{class:'hw'},
    el('dl',{class:'hw-metrics'},metrics.map(([label,value])=>el('div',{},el('dt',{},label),el('dd',{},String(value))))),
    layout,
    el('div',{class:'hw-batch-stats'},
      disclosure('Batch statistics',moreMetrics,showReferenceStatistics(placement)))));

  function display(id) {
    const row = all.get(id);
    if (!row) return id;
    return row.origin==='schema_root' ? typeLabel(row.kind) : row.name;
  }
  function matches(node) {
    return (!kind || kind===node.kind) &&
      [node.name,node.id,node.kind,typeLabel(node.kind),...(node.aliases||[])].join(' ').toLowerCase().includes(query);
  }
  function pathToRoot(id) {
    const path = [id];
    let next = parents.get(id)?.[0];
    while (next && !path.includes(next.parent_id)) {
      path.unshift(next.parent_id);
      next=parents.get(next.parent_id)?.[0];
    }
    return path;
  }
  function typeOnly(edge) {
    return children.get(edge.child_id).length===0 && parents.get(edge.child_id).length===1;
  }
  function rememberPath(id) {
    if (mode==='dag') {
      if (dagMode==='focus') {
        rememberFocus(id);
        return;
      }
      const pending = [id];
      const seen = new Set();
      while (pending.length) {
        const next = pending.pop();
        if (seen.has(next)) continue;
        seen.add(next);
        for (const edge of parents.get(next) || []) {
          dagExpanded.add(edge.parent_id);
          pending.push(edge.parent_id);
        }
      }
      return;
    }
    const path = pathToRoot(id);
    for (let i=1;i<path.length;i+=1) opened.add(path.slice(0,i).join('/'));
    const parent = parents.get(id)?.[0];
    if (path.length===2 && parent && typeOnly(parent)) typeOnlyOpened.add(path[0]);
  }
  function clearSearch() { query=''; search.value=''; }
  function showOverview() {
    mode='overview';
    kind=''; kindControl.value='';
    clearSearch();
    refresh();
    tabs.get('overview').focus({preventScroll:true});
  }
  function openType(root) {
    mode='tree';
    kind=placed.get(root).kind; kindControl.value=kind;
    current=root;
    opened.add(root);
    clearSearch();
    refresh();
    revealCurrent(true);
    revealContextOnMobile();
  }
  function openFocus(id) {
    mode='dag';
    dagMode='focus';
    clearSearch();
    focusDag(id);
    revealContextOnMobile();
  }
  function revealContextOnMobile() {
    if (window.matchMedia('(max-width: 900px)').matches) context.scrollIntoView({block:'start'});
  }
  function rememberFocus(id) {
    if (!placed.has(id) || id===dagFocus) return;
    dagHistory.push(dagFocus);
    dagFocus=id;
  }
  function focusDag(id) {
    const visible = new Set([...main.querySelectorAll('.hw-dag [data-node]')].map(node=>node.dataset.node));
    rememberFocus(id);
    current=id;
    renderMain(visible);
    renderDetail();
    revealCurrent(true);
  }
  function restoreDagFocus(id, control) {
    dagFocus=id;
    current=id;
    refresh();
    revealCurrent();
    main.querySelector(control)?.focus({preventScroll:true});
  }
  function switchDagMode(next) {
    if (dagMode===next) return;
    dagMode=next;
    if (current && placed.has(current)) rememberPath(current);
    refresh();
    revealCurrent();
    main.querySelector('.hw-dag-mode[aria-pressed="true"]').focus({preventScroll:true});
  }
  function focusDetail() {
    if (!window.matchMedia('(max-width: 900px)').matches) return;
    detail.focus({preventScroll:true});
    detail.scrollIntoView({block:'start'});
  }
  function jumpNode(id) {
    current=id;
    if (!placed.has(id)) mode='unplaced';
    else if (mode==='overview' || !isHierarchy()) { mode='dag'; dagMode='focus'; }
    clearSearch();
    if (kind && all.get(id)?.kind!==kind) { kind=''; kindControl.value=''; }
    rememberPath(id);
    refresh();
    revealCurrent(true);
    focusDetail();
  }
  function revealCurrent(focus=false) {
    const viewport = main.querySelector('.hw-dag, .hw-chart, .hw-forest, .hw-flat');
    const active = [...main.querySelectorAll('[data-node]')].find(node=>node.dataset.node===current);
    if (!viewport || !active) return;
    const bounds = viewport.getBoundingClientRect();
    const target = active.getBoundingClientRect();
    if (target.left < bounds.left) viewport.scrollLeft += target.left - bounds.left;
    else if (target.right > bounds.right) viewport.scrollLeft += target.right - bounds.right;
    if (target.top < bounds.top) viewport.scrollTop += target.top - bounds.top;
    else if (target.bottom > bounds.bottom) viewport.scrollTop += target.bottom - bounds.bottom;
    if (focus) active.focus({preventScroll:true});
  }
  function pick(id) {
    current=id;
    for (const row of main.querySelectorAll('[data-node]')) {
      const active = row.dataset.node === current;
      (row.closest('.hw-chart-node, .hw-dag-node') || row).classList.toggle('hw-current',active);
      row.setAttribute('aria-pressed',String(active));
    }
    for (const link of main.querySelectorAll('.hw-dag-link')) {
      link.classList.toggle('hw-dag-active',link.dataset.parent===current || link.dataset.child===current);
    }
    if (mode!=='dag') renderContext();
    renderDetail();
    focusDetail();
  }
  function pickDagLabel(event, id, canExplore) {
    clearTimeout(dagPickTimer);
    if (!canExplore || event.detail===0 || event.pointerType==='touch') {
      pick(id);
    } else if (event.detail===1) {
      const control = event.currentTarget;
      // Updating the detail pane can move the graph between the two clicks.
      dagPickTimer = setTimeout(()=> {
        if (control.isConnected && main.contains(control)) pick(id);
      },350);
    }
  }
  function toggleBranch(key) {
    const viewport = main.querySelector('.hw-chart, .hw-forest');
    const {scrollTop,scrollLeft} = viewport;
    if (opened.has(key)) opened.delete(key);
    else opened.add(key);
    renderMain();
    const next = main.querySelector('.hw-chart, .hw-forest');
    next.scrollTop=scrollTop;
    next.scrollLeft=scrollLeft;
    [...next.querySelectorAll('[data-branch]')].find(row=>row.dataset.branch===key)?.focus({preventScroll:true});
  }
  function branch(id, path=[], edge=null) {
    const trail = [...path,id];
    const key = trail.join('/');
    const kids = children.get(id);
    const open = opened.has(key);
    const action = button('',()=>pick(id),'hw-node');
    action.dataset.node=id;
    action.setAttribute('aria-pressed',String(id===current));
    if (id===current) action.classList.add('hw-current');
    action.append(...[el('span',{class:'hw-node-name'},display(id)),
      parents.get(id).length>1 && status(`${parents.get(id).length} parents`),
      (!edge || kids.length>0) && el('span',{class:'hw-tree-count'},edge ?
        countLabel(kids.length,'child','children') : countLabel(typeMap(id).positions.size,'concept','concepts'))].filter(Boolean));
    let toggle = el('span',{class:'hw-branch-dot','aria-hidden':'true'});
    if (kids.length) {
      toggle = button(open?'▾':'▸',()=>toggleBranch(key),'hw-toggle');
      toggle.dataset.branch=key;
      toggle.setAttribute('aria-expanded',String(open));
      toggle.setAttribute('aria-label',(open?'Collapse ':'Expand ')+display(id));
    }
    const item = el('li',{class:'hw-branch'+(edge?'':' hw-root')},el('div',{class:'hw-branch-row'},toggle,action));
    if (open && kids.length) {
      const sorted = kids.filter(row=>!trail.includes(row.child_id)).sort((a,b)=>
        display(a.child_id).localeCompare(display(b.child_id)));
      const rows = edge ? sorted : sorted.filter(row=>!typeOnly(row));
      const typeOnlyRows = edge ? [] : sorted.filter(typeOnly);
      const group = typeOnlyRows.length>0 && el('details',{
        class:'hw-type-only',open:typeOnlyOpened.has(id),onToggle:event=> {
          if (!event.currentTarget.isConnected) return;
          if (event.currentTarget.open) typeOnlyOpened.add(id);
          else typeOnlyOpened.delete(id);
        },
      },el('summary',{},`Only linked to type root (${typeOnlyRows.length})`),
      el('ul',{class:'hw-children'},typeOnlyRows.map(row=>branch(row.child_id,trail,row))));
      item.append(el('ul',{class:'hw-children'},rows.map(row=>branch(row.child_id,trail,row)),
        group && el('li',{},group)));
    }
    return item;
  }
  function chartBranch(id, path=[]) {
    const trail = [...path,id];
    const key = trail.join('/');
    const label = display(id);
    const kids = children.get(id).filter(edge=>!trail.includes(edge.child_id));
    const parentCount = parents.get(id).length;
    const open = kids.length>0 && opened.has(key);
    const selected = id===current;
    const name = el('button',{
      type:'button', class:'hw-chart-label', title:label, dataset:{node:id},
      'aria-pressed':String(selected), onClick:()=>pick(id),
      onDblClick:kids.length ? ()=>toggleBranch(key) : null,
    },el('span',{class:'hw-chart-name'},label),
    parentCount>1 && el('span',{class:'hw-chart-meta'},`${parentCount} parents`));
    const toggle = kids.length ? el('button',{
      type:'button', class:'hw-chart-toggle', dataset:{branch:key},
      'aria-label':(open?'Collapse ':'Expand ')+label, 'aria-expanded':String(open),
      title:countLabel(kids.length,'direct child','direct children'), onClick:()=>toggleBranch(key),
    },open?'−':'+') : null;
    const nodeClass = ['hw-chart-node',!path.length && 'hw-chart-root',open && 'hw-chart-open',selected && 'hw-current']
      .filter(Boolean).join(' ');
    return el('li',{class:'hw-chart-branch'},el('div',{class:nodeClass},name,toggle),
      open && el('ul',{class:'hw-chart-children'},kids.map(edge=>chartBranch(edge.child_id,trail))));
  }
  function dagCard(id) {
    return [...main.querySelectorAll('.hw-dag-node')].find(node=>node.dataset.dagNode===id);
  }
  function toggleDag(id, control) {
    const viewport = main.querySelector('.hw-dag');
    const anchor = dagCard(id);
    const {scrollLeft,scrollTop} = viewport;
    const {offsetLeft,offsetTop} = anchor;
    const visible = new Set([...viewport.querySelectorAll('[data-node]')].map(node=>node.dataset.node));
    const opening = !dagExpanded.delete(id);
    if (opening) dagExpanded.add(id);
    renderMain(opening ? visible : null);
    const next = main.querySelector('.hw-dag');
    const replacement = dagCard(id);
    next.scrollLeft=scrollLeft+replacement.offsetLeft-offsetLeft;
    next.scrollTop=scrollTop+replacement.offsetTop-offsetTop;
    const child = opening && children.get(id).find(edge=>!visible.has(edge.child_id));
    if (child) {
      const preview = dagCard(child.child_id).offsetLeft + Math.min(88,next.clientWidth/4);
      next.scrollLeft += Math.max(0,preview-next.clientWidth-next.scrollLeft);
    }
    replacement.querySelector(control).focus({preventScroll:true});
  }
  function dagSegment(x1, y1, x2, y2) {
    return el('span',{class:'hw-dag-segment',style:{
      left:`${Math.min(x1,x2)}px`, top:`${Math.min(y1,y2)}px`,
      width:`${Math.max(1,Math.abs(x2-x1))}px`, height:`${Math.max(1,Math.abs(y2-y1))}px`,
    }});
  }
  function dagLink(edge, positions, nodeWidth, nodeHeight, targetOffset) {
    const {parent_id:parent,child_id:child} = edge;
    const from = positions.get(parent);
    const to = positions.get(child);
    const x1 = from.x + nodeWidth;
    const x2 = to.x;
    const midX = x2 - Math.min(22,(x2-x1)/2) - Math.abs(targetOffset)*.4;
    const y1 = from.y + nodeHeight/2;
    const y2 = to.y + nodeHeight/2 + targetOffset;
    // Links spanning a column run through the gap below the parent's row.
    const points = x2 - x1 > nodeWidth ?
      [[x1,y1],[x1+8,y1],[x1+8,from.y+nodeHeight+6],[midX,from.y+nodeHeight+6],[midX,y2],[x2,y2]] :
      [[x1,y1],[midX,y1],[midX,y2],[x2,y2]];
    const active = current===parent || current===child;
    return el('div',{
      class:'hw-dag-link'+(active?' hw-dag-active':''), dataset:{parent,child}, 'aria-hidden':'true',
    },points.slice(1).map(([x,y],i)=>dagSegment(...points[i],x,y)),
    el('span',{class:'hw-dag-arrow',style:{left:`${x2-6}px`,top:`${y2-3}px`}}));
  }
  function renderDag(shownRoots, previous) {
    const focusView = dagMode==='focus';
    const neighborhood = focusView && dagFocus;
    const {positions,links,width,height,nodeWidth,nodeHeight,columns=[]} = neighborhood ?
      layoutDagNeighborhood({focus:dagFocus,parents,children}) : layoutDag({
        roots:shownRoots,children,parents,expanded:focusView ? new Set() : dagExpanded,
      });
    const world = el('div',{class:'hw-dag-world',style:{width:`${width}px`,height:`${height}px`}});
    const labels = {parents:'Direct parents',focus:'Selected concept',children:'Direct children'};
    for (const {role,x} of columns) world.append(el('div',{
      class:'hw-dag-column-label',dataset:{role},style:{left:`${x}px`,top:'13px',width:`${nodeWidth}px`},
    },labels[role]));
    const arrivals = new Map();
    for (const edge of links) {
      if (!arrivals.has(edge.child_id)) arrivals.set(edge.child_id,[]);
      arrivals.get(edge.child_id).push(edge);
    }
    for (const incoming of arrivals.values()) {
      incoming.sort((a,b)=>positions.get(a.parent_id).y-positions.get(b.parent_id).y);
      // Relations sharing a focus endpoint use a common stem.
      const gap = !neighborhood && incoming.length>1 ? Math.min(9,32/(incoming.length-1)) : 0;
      incoming.forEach((edge,i)=>world.append(
        dagLink(edge,positions,nodeWidth,nodeHeight,(i-(incoming.length-1)/2)*gap)));
    }
    for (const [id,{x,y}] of positions) {
      const label = display(id);
      const kids = children.get(id).length;
      const open = dagExpanded.has(id);
      const parentCount = parents.get(id).length;
      const chosen = current===id;
      const focal = neighborhood && id===dagFocus;
      const canExplore = focusView ? !focal && (kids>0 || parentCount>0) : kids>0;
      const cls = ['hw-dag-node',shownRoots.includes(id) && 'hw-dag-root',chosen && 'hw-current',
        focal && 'hw-dag-focus',previous && !previous.has(id) && 'hw-dag-revealed'].filter(Boolean).join(' ');
      const name = el('button',{
        type:'button',class:'hw-chart-label',title:label,dataset:{node:id},'aria-pressed':String(chosen),
        onClick:event=>pickDagLabel(event,id,canExplore),onDblClick:canExplore ? ()=>explore('.hw-chart-label') : null,
      },el('span',{class:'hw-chart-name'},label),
      parentCount>1 && el('span',{class:'hw-chart-meta'},`${parentCount} parents`));
      const description = focusView ? `Show direct relations for ${label}` : (open?'Collapse ':'Expand ')+label;
      function explore(control) {
        clearTimeout(dagPickTimer);
        if (focusView) focusDag(id);
        else toggleDag(id,control);
      }
      const toggle = canExplore ? el('button',{
        type:'button',class:'hw-chart-toggle','aria-label':description,
        'aria-expanded':focusView ? null : String(open),
        title:focusView ? description : countLabel(kids,'direct child','direct children'),onClick:()=>explore('.hw-chart-toggle'),
      },focusView && neighborhood ? '›' : open && !focusView ? '−' : '+') : null;
      world.append(el('div',{class:cls,dataset:{dagNode:id},style:{
        left:`${x}px`,top:`${y}px`,width:`${nodeWidth}px`,height:`${nodeHeight}px`,
      }},name,toggle));
    }
    const controls = [];
    if (focusView && dagFocus) controls.push(
      el('button',{type:'button',class:'hw-dag-reset','data-dag-back':'',disabled:!dagHistory.length,
        onClick:()=>restoreDagFocus(dagHistory.pop(),dagHistory.length?'[data-dag-back]':'.hw-dag-mode[aria-pressed="true"]')},'Back'),
      el('button',{type:'button',class:'hw-dag-reset',onClick:()=> {
        dagHistory.length=0;
        restoreDagFocus(null,'.hw-dag-mode[aria-pressed="true"]');
      }},'Back to roots'),
    );
    if (!focusView) controls.push(button('Collapse all',()=> {
      dagExpanded.clear();
      renderMain();
      main.querySelector('.hw-dag-reset').focus({preventScroll:true});
    },'hw-dag-reset'));
    const modes = [['focus','Focus view'],['overview','Branch view']].map(([id,label])=>el('button',{
      type:'button',class:'hw-dag-mode','aria-pressed':String(id===dagMode),onClick:()=>switchDagMode(id),
    },label));
    const help = focusView ? 'Use a node’s side button to show its direct relations.' :
      'Use + to expand a branch.';
    main.replaceChildren(
      el('div',{class:'hw-dag-nav'},el('div',{role:'group','aria-label':'DAG view'},modes),el('div',{},controls)),
      el('div',{class:'hw-dag-tools'},el('p',{class:'hw-dag-help'},help),
        el('span',{class:'hw-dag-count','aria-live':'polite'},countLabel(positions.size,'node','nodes'))),
      el('div',{class:'hw-dag'+(neighborhood?' hw-dag-neighborhood':''),role:'region','aria-label':'Entity DAG',tabindex:0},world));
  }
  function typeMap(root) {
    if (!typeMaps.has(root)) typeMaps.set(root,layoutTaxonomyMap({root,children,parents}));
    return typeMaps.get(root);
  }
  function drawTypeMap({width,height,positions,links}, selectedId) {
    const ratio = Math.max(2,window.devicePixelRatio || 1);
    const canvas = el('canvas',{class:'hw-taxonomy-map',width:width*ratio,height:height*ratio,'aria-hidden':'true'});
    const drawing = canvas.getContext('2d');
    drawing.scale(canvas.width/width,canvas.height/height);
    const linked = new Set();
    drawing.strokeStyle='#7b9ab5';
    drawing.lineWidth=.8;
    drawing.beginPath();
    for (const {parent_id,child_id} of links) {
      const from = positions.get(parent_id);
      const to = positions.get(child_id);
      drawing.moveTo(from.x,from.y);
      drawing.lineTo(to.x,to.y);
      linked.add(parent_id);
      linked.add(child_id);
    }
    drawing.stroke();
    for (const [id,{x,y}] of positions) {
      drawing.fillStyle = linked.has(id) ? '#275c89' : '#808e9c';
      drawing.beginPath();
      drawing.arc(x,y,2.55,0,2*Math.PI);
      drawing.fill();
    }
    const active=positions.get(selectedId);
    if (active) {
      drawing.strokeStyle='#1c5a84';
      drawing.lineWidth=1.5;
      drawing.beginPath();
      drawing.arc(active.x,active.y,4.6,0,2*Math.PI);
      drawing.stroke();
    }
    return canvas;
  }
  function renderOverview(shownRoots) {
    const cards = shownRoots.map(root=> {
      const map=typeMap(root);
      const label=display(root);
      const counts=[countLabel(map.positions.size,'concept','concepts'),countLabel(map.links.length,'edge','edges')];
      return el('button',{type:'button',class:'hw-taxonomy-type',
        'aria-label':`Explore ${label}, ${counts.join(', ')}`,onClick:()=>openType(root)},
      el('span',{class:'hw-taxonomy-name'},label),drawTypeMap(map),
      el('span',{class:'hw-taxonomy-counts'},counts.map(count=>el('span',{},count))));
    });
    main.replaceChildren(el('section',{class:'hw-taxonomy-overview','aria-label':'Taxonomy by entity type'},
      el('div',{class:'hw-taxonomy-heading'},el('h3',{},'Taxonomy by entity type'),
        el('p',{class:'hw-note'},'Each dot is a concept. Lines show parent–child edges. Select a type to browse its tree.')),
      el('div',{class:'hw-taxonomy-grid'},cards)));
  }
  function renderContext() {
    const inFocus=mode==='dag' && dagMode==='focus' && dagFocus;
    const showContext=isHierarchy() && mode!=='overview' && !query;
    context.hidden=!showContext;
    context.replaceChildren();
    if (!showContext) return;
    const node=placed.get(inFocus || current);
    const root=roots[node?.kind || kind];
    const separator=()=>el('span',{'aria-hidden':'true'},'›');
    const controls=[button('Overview',showOverview,'hw-context-button')];
    if (root) controls.push(separator(),button(display(root),()=>openType(root),'hw-context-button'));
    if (inFocus && dagFocus!==root) {
      for (const id of pathToRoot(dagFocus).slice(1)) {
        controls.push(separator(),id===dagFocus ?
          el('span',{class:'hw-context-current','aria-current':'location'},display(id)) :
          button(display(id),()=>focusDag(id),'hw-context-button'));
      }
    }
    const map=root && typeMap(root);
    context.append(el('nav',{class:'hw-context','aria-label':inFocus ? 'One path from the entity type root' : 'Ontology location'},controls,
      map && mode==='dag' && el('span',{class:'hw-context-count'},countLabel(map.positions.size,'concept in type','concepts in type'))));
    if (inFocus && dagFocus!==root && map) {
      context.append(el('details',{class:'hw-focus-map',open:showTypeMap,onToggle:event=> {
        if (event.currentTarget.isConnected) showTypeMap=event.currentTarget.open;
      }},el('summary',{},`${display(root)} map`),
      el('div',{class:'hw-focus-map-body'},drawTypeMap(map,dagFocus),
        el('p',{class:'hw-note'},`The ring marks ${display(dagFocus)} in the full ${display(root).toLowerCase()} view.`))));
    }
  }
  function renderMain(previous=null) {
    layout.dataset.view=mode;
    for (const [name,control] of tabs) control.setAttribute('aria-pressed',String(name===mode));
    labelMoreViews();
    renderContext();
    if (isHierarchy() && !query) {
      const shownRoots = Object.entries(roots).filter(([key])=>!kind || key===kind).map(([,id])=>id);
      if (!shownRoots.length) {
        main.replaceChildren(el('p',{class:'hw-note hw-empty'},'This batch has no placed concepts.'));
      } else if (mode==='overview') {
        renderOverview(shownRoots);
      } else if (mode==='dag') {
        renderDag(shownRoots,previous);
      } else if (mode==='tree') {
        main.replaceChildren(el('ul',{class:'hw-forest'},shownRoots.map(id=>branch(id))));
      } else {
        main.replaceChildren(
          el('p',{class:'hw-chart-help'},'Select a name for details. Double-click it or select + to expand its children.'),
          el('div',{class:'hw-chart',role:'region','aria-label':'Entity hierarchy tree',tabindex:0},
            el('ul',{class:'hw-chart-roots'},shownRoots.map(id=>chartBranch(id)))));
      }
      return;
    }
    const rows = (mode==='steps' ? decisions.map(row=>all.get(row.concept_id)) : mode==='unplaced' ? unplaced : [...all.values()]).filter(matches);
    const list = el('div',{class:'hw-flat'});
    for (const row of rows) {
      const outcome=outcomes.get(row.id);
      const ctl=button(el('span',{class:'hw-edge-names'},mode==='steps' && el('small',{class:'hw-sub'},String(outcome.step)),
        el('strong',{},display(row.id))),()=>isHierarchy() ? jumpNode(row.id) : pick(row.id),'hw-edge');
      ctl.dataset.node=row.id;
      ctl.setAttribute('aria-pressed',String(row.id===current));
      if (row.id===current) ctl.classList.add('hw-current');
      if (outcome) ctl.append(chip(actions,outcome.decision.action));
      list.append(ctl);
    }
    main.replaceChildren(rows.length ? list : el('p',{class:'hw-note hw-empty'},
      mode==='unplaced' && !unplaced.length ? 'All concepts in this batch have been placed.' : 'No concepts match the current filters.'));
  }
  function links(title, entries, key) {
    if (!entries.length) return null;
    return el('div',{class:'hw-source'},el('h4',{},title),entries.map(edge=>
      el('div',{class:'hw-relation-link'},button(display(edge[key]),()=>jumpNode(edge[key]),'btn hw-link'),chip(supportLabels,edge.support))));
  }
  function showLegacyGo(reference) {
    if (reference?.placement_evidence) return showReference({...reference,result:reference.placement_evidence});
    const item = reference?.result;
    if (!item) return null;
    return el('div',{class:'hw-source'},el('h4',{},'GO Reference'),chip(referenceStatuses,item.status),
      item.go_name && el('p',{lang:'en'},item.go_name),item.go_definition && el('p',{lang:'en'},item.go_definition),
      evidenceLink(item.source,item.go_id || 'OLS record'),
      ...(item.ontology_relations||[]).map(row=>el('p',{},`${row.subject} · ${row.predicate} · ${row.object}`)),
      ...(reference.reviews||[]).map(row=>showReview(row)));
  }
  function showReference(reference) {
    const item = reference?.result;
    if (!item) return null;
    const terms = (item.terms || []).map(term=>el('li',{},
      el('div',{class:'hw-relation-link'},el('strong',{lang:'en'},term.name),
        evidenceLink(term.source_url,term.term_id) || el('code',{},term.term_id)),
      term.definition && el('p',{lang:'en'},term.definition)));
    const facts = (item.relations || []).map(row=>el('li',{},
      el('span',{},`${row.child_id} · ${row.predicate} · ${row.parent_id}`),
      evidenceLink(row.source_url,'Relation source')));
    return el('div',{class:'hw-source'},el('h4',{},'Entity Reference'),
      el('div',{class:'hw-relation-link'},chip(referenceStatuses,item.status),
        item.source && el('span',{class:'hw-note'},item.source),
        referenceReviewStatuses[reference.review_status] &&
          el('span',{class:'hw-note'},referenceReviewStatuses[reference.review_status])),
      item.reason && el('p',{class:'hw-note'},modelNote(referenceReasons[item.reason] || item.reason)),
      terms.length>0 && el('ul',{class:'hw-reference-list','aria-label':'Source terms'},terms),
      facts.length>0 && el('ul',{class:'hw-reference-list','aria-label':'External ontology relations'},facts),
      ...(reference.reviews||[]).map(row=>showReview(row,true)));
  }
  function showComparisons(decision) {
    if (!decision.comparisons?.length) return null;
    const list=el('div',{class:'hw-comparisons'});
    const panel=disclosure(`Comparison reviews (${decision.comparisons.length})`,list);
    panel.addEventListener('toggle',()=> {
      if (!panel.open || list.hasChildNodes()) return;
      for (const row of decision.comparisons) {
        const assisted = row.reference_reviews?.length ? row.reference_reviews : row.go_reviews || [];
        list.append(el('section',{},
          el('div',{class:'hw-relation-link'},button(display(row.candidate_id),()=>jumpNode(row.candidate_id),'btn hw-link'),status(relations[row.result] || row.result)),
          row.proposal?.reason && el('p',{},modelNote(row.proposal.reason)),
          ...(row.reviews||[]).map(review=>showReview(review)),
          ...assisted.map(review=>showReview(review,true))));
      }
    });
    return panel;
  }
  function placementRecord(node,record) {
    const decision=record?.decision;
    const items = [
      el('p',{class:'hw-detail-id'},node.id),
      node.origin!=='schema_root' && el('p',{lang:'en'},modelNote(node.definition,'Download original definition')),
      record && el('p',{class:'hw-note'},`Step ${record.step}`),
      ['needs_review','rejected','equivalent_pending'].includes(decision?.action) && decision.reason &&
        el('p',{class:'hw-rationale'},modelNote(decision.reason)),
    ];
    if (decision) {
      for (const [label,key] of [['Added edges','added_edges'],['Removed redundant edges','removed_edges']]) {
        if (decision[key]?.length) items.push(el('div',{class:'hw-source'},el('h4',{},label),decision[key].map(row=>
          el('p',{},display(row.child_id)+' is-a '+display(row.parent_id)))));
      }
      if (decision.generated_parent_reviews?.length) items.push(el('div',{class:'hw-source'},
        el('h4',{},'Proposed Intermediate Parent'),decision.generated_parent_reviews.map(review=>showReview(review))));
      const reference = showReference(decision.entity_reference) || showLegacyGo(decision.go_reference);
      if (reference) items.push(reference);
      items.push(showComparisons(decision));
    }
    return disclosure('Placement record',items);
  }
  function renderDetail() {
    const node=all.get(current);
    if (!node) {
      detail.replaceChildren(el('h3',{},'Inspect a concept'),
        el('p',{class:'hw-note'},'Select a node or search by name.'));
      return;
    }
    const record=outcomes.get(node.id);
    const directParents=parents.get(node.id)||[];
    const directChildren=children.get(node.id)||[];
    const sourceCount=node.source_mentions?.length || 0;
    const isRoot = node.origin === 'schema_root';
    const relationCount = [
      !isRoot && countLabel(directParents.length,'direct parent','direct parents'),
      countLabel(directChildren.length,'direct child','direct children'),
    ].filter(Boolean).join(' · ');
    detail.replaceChildren(...[el('div',{class:'hw-detail-header'},
      el('h3',{},display(node.id)),
      (!isRoot || record) && el('div',{class:'hw-relation-link'},
        !isRoot && el('span',{class:'hw-note'},typeLabel(node.kind)),
        record && chip(actions,record.decision.action)),
      placed.has(node.id) && !isRoot && (mode!=='dag' || dagMode!=='focus' || dagFocus!==node.id) &&
        button('Focus in DAG',()=>openFocus(node.id),'btn hw-link')),
    directParents.length || directChildren.length ?
      disclosure(relationCount,links('Direct parents',directParents,'parent_id'),links('Direct children',directChildren,'child_id')) :
      el('p',{class:'hw-note'},'No direct parents or children.'),
    sourceCount ? disclosure(`Source mentions (${sourceCount})`,
      node.source_mentions.map(id=>button(mentions.get(id)?.mention || id,()=>go('extraction',id),'btn hw-link')),
      el('div',{class:'hw-evidence-path'},button('View deduplicated concept',()=>go('deduplication',node.id),'btn hw-link'),
        button('View order',()=>go('ordering',node.id),'btn hw-link'))) : null,
    !isRoot && placementRecord(node,record)].filter(Boolean));
  }
  function refresh() { renderMain(); renderDetail(); }
  if (current) {
    mode=placed.has(current)?'dag':'unplaced';
    rememberPath(current);
  }
  refresh();
  revealCurrent();
}
