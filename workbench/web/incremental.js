import { el, status, typeLabel } from './ui.js';
import { layoutDag, layoutDagNeighborhood } from './dag-layout.js';

const actions = {
  new_under_root:['挂到类型根','neutral'], as_child:['挂到已有父类下','good'],
  as_parent:['成为已有概念的父类','good'], insert_between:['插入中间层','good'],
  needs_review:['待复核','warn'], equivalent_pending:['可能重复 · 待复核','warn'], rejected:['拒绝放置','bad'],
};
const supportLabels = {
  schema_type:['类型根','neutral'], dual_model_review:['模型复审支持','good'], go_informed_review:['GO 辅助复审','good'],
  reference_informed_review:['外部证据辅助复审','good'],
};
const relations = {narrower:'更具体',broader:'更宽',equivalent:'等价',unrelated:'未发现类包含',uncertain:'待复核',needs_review:'待复核',rejected:'复审未通过'};
const referenceStatuses = {
  MATCHED:['匹配','good'], REFERENCE_ONLY:['参考条目','neutral'], NO_MATCH:['未找到匹配','neutral'],
  UNCERTAIN:['证据不足','warn'], LOOKUP_FAILED:['查询失败','bad'],
};
const referenceReviewStatuses = {
  reviewed:'已复审', no_usable_reference:'暂无可用引用', invalid_review:'回复格式无法校验',
  review_failed:'复审失败', identity_only:'用于核对身份',
};
const reviewLabels = {true:'复审支持',false:'复审未通过',null:'证据不足'};

const button = (label,onClick,cls='btn') => el('button',{class:cls,type:'button',onClick},label);
const chip = (labels,key) => status(...(labels[key] || [key,'neutral']));
const evidenceHosts = new Set(['www.ebi.ac.uk','www.genenames.org','hgnc.genenames.org','rest.genenames.org','purl.obolibrary.org']);

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
  let mode = 'dag';
  let current = all.has(selected) ? selected : undefined;
  let query = '';
  let kind = '';
  let dagMode = 'focus';
  let dagFocus = null;
  let dagPickTimer;
  const dagHistory = [];
  const isHierarchy = () => ['dag','tree','outline'].includes(mode);
  const opened = new Set();
  const dagExpanded = new Set();
  const tabs = new Map();
  const search = el('input',{type:'search',class:'hw-search','aria-label':'按名称或类型搜索',placeholder:'名称、别名或类型'});
  const kindControl = el('select',{class:'hw-kind','aria-label':'实体类型'},
    el('option',{value:''},'全部类型'),
    Object.keys(roots).map(key=>el('option',{value:key},typeLabel(key))));
  const main = el('section',{class:'hw-main','aria-label':'层级浏览区'});
  const detail = el('aside',{class:'hw-detail','aria-label':'逐个放置详情',tabindex:-1});
  const metrics = [
    ['入树概念', source.length-unplaced.length],
    ['具体父子边', specific],
    ['多父类概念', multiParents],
    ['未挂接概念', unplaced.length],
  ];
  if (placement.entity_reference_count != null) metrics.push(['实体查证',placement.entity_reference_count]);
  else if (placement.go_reference_count) metrics.push(['GO 查询',placement.go_reference_count]);
  const toolbar = el('div',{class:'hw-toolbar'});
  const tabList = el('div',{class:'hw-tabs'});
  const views = [['dag','DAG 图'],['tree','树状图'],['outline','层级列表'],['steps',`放置记录 · ${decisions.length}`],['unplaced',`未挂接 · ${unplaced.length}`]];
  for (const [id,label] of views) {
    const control = button(label,()=> {
      if (mode===id) return;
      mode=id;
      if (isHierarchy() && current) rememberPath(current);
      refresh();
      revealCurrent();
    },'hw-tab');
    tabs.set(id,control);
    tabList.append(control);
  }
  toolbar.append(tabList);
  search.addEventListener('input',()=> { query=search.value.trim().toLowerCase(); renderMain(); });
  kindControl.addEventListener('change',()=> {
    kind=kindControl.value;
    if (kind && dagFocus && all.get(dagFocus).kind!==kind) {
      dagFocus=null;
      dagHistory.length=0;
    }
    if (kind && all.get(current)?.kind!==kind) {
      current = isHierarchy() && !query ? roots[kind] :
        (mode==='unplaced' ? unplaced : source).find(matches)?.id;
    }
    refresh();
  });
  const layout = el('div',{class:'hw-layout'},
      el('section',{class:'hw-browser'},toolbar,el('div',{class:'hw-filters'},
        el('label',{class:'hw-field'},el('span',{},'搜索概念'),search),
        el('label',{class:'hw-field'},el('span',{},'实体类型'),kindControl)),
      main),detail);
  mount.replaceChildren(el('div',{class:'hw'},
    el('dl',{class:'hw-metrics'},metrics.map(([label,value])=>el('div',{},el('dt',{},label),el('dd',{},String(value))))),
    layout));

  function display(id) {
    const row = all.get(id);
    if (!row) return id;
    return row.origin==='schema_root' ? typeLabel(row.kind) : row.name;
  }
  function matches(node) {
    return (!kind || kind===node.kind) &&
      [node.name,node.id,node.kind,typeLabel(node.kind),...(node.aliases||[])].join(' ').toLowerCase().includes(query);
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
    const path = [id];
    let next = parents.get(id)?.[0];
    while (next && !path.includes(next.parent_id)) {
      path.unshift(next.parent_id);
      next=parents.get(next.parent_id)?.[0];
    }
    for (let i=1;i<path.length;i+=1) opened.add(path.slice(0,i).join('/'));
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
    else if (!isHierarchy()) mode='dag';
    query=''; search.value='';
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
      edge && chip(supportLabels,edge.support),
      parents.get(id).length>1 && status(`${parents.get(id).length} 个父类`),
      kids.length>0 && el('span',{class:'hw-count'},String(kids.length))].filter(Boolean));
    let toggle = el('span',{class:'hw-branch-dot','aria-hidden':'true'});
    if (kids.length) {
      toggle = button(open?'▾':'▸',()=>toggleBranch(key),'hw-toggle');
      toggle.dataset.branch=key;
      toggle.setAttribute('aria-expanded',String(open));
      toggle.setAttribute('aria-label',(open?'折叠':'展开')+display(id));
    }
    const item = el('li',{class:'hw-branch'+(edge?'':' hw-root')},el('div',{class:'hw-branch-row'},toggle,action));
    if (open && kids.length) item.append(el('ul',{class:'hw-children'},kids.filter(row=>!trail.includes(row.child_id)).map(row=>branch(row.child_id,trail,row))));
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
    parentCount>1 && el('span',{class:'hw-chart-meta'},`${parentCount} 个父类`));
    const toggle = kids.length ? el('button',{
      type:'button', class:'hw-chart-toggle', dataset:{branch:key},
      'aria-label':(open?'折叠':'展开')+label, 'aria-expanded':String(open),
      title:`${kids.length} 个直接子类`, onClick:()=>toggleBranch(key),
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
    const labels = {parents:'直接父类',focus:'当前概念',children:'直接子类'};
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
      parentCount>1 && el('span',{class:'hw-chart-meta'},`${parentCount} 个父类`));
      const description = focusView ? `查看${label}的直接关系` : (open?'折叠':'展开')+label;
      function explore(control) {
        clearTimeout(dagPickTimer);
        if (focusView) focusDag(id);
        else toggleDag(id,control);
      }
      const toggle = canExplore ? el('button',{
        type:'button',class:'hw-chart-toggle','aria-label':description,
        'aria-expanded':focusView ? null : String(open),
        title:focusView ? description : `${kids} 个直接子类`,onClick:()=>explore('.hw-chart-toggle'),
      },focusView && neighborhood ? '›' : open && !focusView ? '−' : '+') : null;
      world.append(el('div',{class:cls,dataset:{dagNode:id},style:{
        left:`${x}px`,top:`${y}px`,width:`${nodeWidth}px`,height:`${nodeHeight}px`,
      }},name,toggle));
    }
    const controls = focusView ? [
      el('button',{type:'button',class:'hw-dag-reset','data-dag-back':'',disabled:!dagHistory.length,
        onClick:()=>restoreDagFocus(dagHistory.pop(),dagHistory.length?'[data-dag-back]':'.hw-dag-mode[aria-pressed="true"]')},'返回上一个'),
      el('button',{type:'button',class:'hw-dag-reset',disabled:!dagFocus,onClick:()=> {
        dagHistory.length=0;
        restoreDagFocus(null,'.hw-dag-mode[aria-pressed="true"]');
      }},'回到根节点'),
    ] : [button('收起全部',()=> {
      dagExpanded.clear();
      renderMain();
      main.querySelector('.hw-dag-reset').focus({preventScroll:true});
    },'hw-dag-reset')];
    const modes = [['focus','关系焦点'],['overview','分支总览']].map(([id,label])=>el('button',{
      type:'button',class:'hw-dag-mode','aria-pressed':String(id===dagMode),onClick:()=>switchDagMode(id),
    },label));
    const help = focusView ? '双击节点或点右侧按钮查看直接关系。' : '双击节点或点 ＋ 展开子类。';
    main.replaceChildren(
      el('div',{class:'hw-dag-nav'},el('div',{role:'group','aria-label':'DAG 浏览方式'},modes),el('div',{},controls)),
      el('div',{class:'hw-dag-tools'},el('p',{class:'hw-dag-help'},help),
        el('span',{class:'hw-dag-count','aria-live':'polite'},`${positions.size} 个节点`)),
      el('div',{class:'hw-dag'+(neighborhood?' hw-dag-neighborhood':''),role:'region','aria-label':'实体 DAG 图',tabindex:0},world));
  }
  function renderMain(previous=null) {
    layout.dataset.view=mode;
    for (const [name,control] of tabs) control.setAttribute('aria-pressed',String(name===mode));
    if (isHierarchy() && !query) {
      const shownRoots = Object.entries(roots).filter(([key])=>!kind || key===kind).map(([,id])=>id);
      if (!shownRoots.length) {
        main.replaceChildren(el('p',{class:'hw-note hw-empty'},'这批文献没有已放置的概念。'));
      } else if (mode==='dag') {
        renderDag(shownRoots,previous);
      } else if (mode==='outline') {
        main.replaceChildren(el('ul',{class:'hw-forest'},shownRoots.map(id=>branch(id))));
      } else {
        main.replaceChildren(
          el('p',{class:'hw-chart-help'},'点 ＋ 或双击名称展开子类，单击名称查看详情。'),
          el('div',{class:'hw-chart',role:'region','aria-label':'实体分类树',tabindex:0},
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
      mode==='unplaced' && !unplaced.length ? '本批次的概念都已挂接。' : '没有符合条件的概念。'));
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
    return el('div',{class:'hw-source'},el('h4',{},'GO 参考'),chip(referenceStatuses,item.status),
      item.go_name && el('p',{lang:'en'},item.go_name),item.go_definition && el('p',{lang:'en'},item.go_definition),
      evidenceLink(item.source,item.go_id || 'OLS 记录'),
      ...(item.ontology_relations||[]).map(row=>el('p',{},`${row.subject} · ${row.predicate} · ${row.object}`)),
      ...(reference.reviews||[]).map(row=>el('p',{},row.reason)));
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
      evidenceLink(row.source_url,'关系来源')));
    return el('div',{class:'hw-source'},el('h4',{},'实体查证'),
      el('div',{class:'hw-relation-link'},chip(referenceStatuses,item.status),
        item.source && el('span',{class:'hw-note'},item.source),
        referenceReviewStatuses[reference.review_status] &&
          el('span',{class:'hw-note'},referenceReviewStatuses[reference.review_status])),
      item.reason && el('p',{class:'hw-note'},item.reason),
      terms.length>0 && el('ul',{class:'hw-reference-list','aria-label':'引用术语'},terms),
      facts.length>0 && el('ul',{class:'hw-reference-list','aria-label':'外部本体关系'},facts),
      ...(reference.reviews||[]).map(row=>el('p',{},reviewLabels[String(row.supported)]+' · '+row.reason)));
  }
  function showComparisons(decision) {
    if (!decision.comparisons?.length) return null;
    const list=el('div',{class:'hw-comparisons'});
    for (const row of decision.comparisons) {
      const assisted = row.reference_reviews?.length ? row.reference_reviews : row.go_reviews || [];
      list.append(el('section',{},
        el('div',{class:'hw-relation-link'},button(display(row.candidate_id),()=>jumpNode(row.candidate_id),'btn hw-link'),status(relations[row.result] || row.result)),
        row.proposal?.reason && el('p',{},row.proposal.reason),
        ...(row.reviews||[]).map(review=>el('p',{},reviewLabels[String(review.supported)]+' · '+review.reason)),
        ...assisted.map(review=>el('p',{},'查证后'+reviewLabels[String(review.supported)]+' · '+review.reason))));
    }
    return el('details',{class:'hw-comparison-audit'},el('summary',{},`与 ${decision.comparisons.length} 个已有概念比较`),list);
  }
  function renderDetail() {
    const node=all.get(current);
    if (!node) {
      detail.replaceChildren(el('h3',{},'放置理由'),el('p',{},'选择概念查看父类、子类、模型判断和文献原句。'));
      return;
    }
    const record=outcomes.get(node.id);
    const decision=record?.decision;
    detail.replaceChildren(el('div',{class:'hw-detail-header'},
      el('p',{class:'hw-detail-id'},node.id),el('h3',{},display(node.id)),
      node.origin!=='schema_root' && el('p',{class:'hw-note'},typeLabel(node.kind)),
      node.origin!=='schema_root' && el('p',{lang:'en'},node.definition),
      record && el('div',{class:'hw-relation-link'},chip(actions,decision.action),el('span',{class:'hw-note'},`第 ${record.step} 次放置`)),
      ['needs_review','rejected','equivalent_pending'].includes(decision?.action) && decision.reason &&
        el('p',{class:'hw-rationale'},decision.reason)),
      ...[links('直接父类',parents.get(node.id)||[],'parent_id'),links('直接子类',children.get(node.id)||[],'child_id')].filter(Boolean));
    if (node.source_mentions?.length) detail.append(el('div',{class:'hw-source'},el('h4',{},'文献原句'),
      node.source_mentions.map(id=>button(mentions.get(id)?.mention || id,()=>go('extraction',id),'btn hw-link')),
      el('div',{class:'hw-evidence-path'},button('查看同义归并',()=>go('deduplication',node.id),'btn hw-link'),
        button('查看排序位置',()=>go('ordering',node.id),'btn hw-link'))));
    if (decision) {
      for (const [label,key] of [['新增连线','added_edges'],['移除的冗余连线','removed_edges']]) {
        if (decision[key]?.length) detail.append(el('div',{class:'hw-source'},el('h4',{},label),decision[key].map(row=>
          el('p',{},display(row.child_id)+' 属于 '+display(row.parent_id)))));
      }
      if (decision.generated_parent_reviews?.length) detail.append(el('div',{class:'hw-source'},
        el('h4',{},'拟议中间类复审'),decision.generated_parent_reviews.map(review=>
          el('p',{},reviewLabels[String(review.supported)]+' · '+review.reason))));
      const reference = showReference(decision.entity_reference) || showLegacyGo(decision.go_reference);
      detail.append(...[reference,showComparisons(decision)].filter(Boolean));
    }
  }
  function refresh() { renderMain(); renderDetail(); }
  if (current) {
    mode=placed.has(current)?'dag':'unplaced';
    rememberPath(current);
  }
  refresh();
  revealCurrent();
}
