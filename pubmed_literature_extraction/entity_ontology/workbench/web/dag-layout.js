const nodeWidth = 172;
const nodeHeight = 64;
const columnGap = 52;
const rowGap = 12;
const padding = 16;

export function layoutDagNeighborhood({focus, parents, children}) {
  const incoming = parents.get(focus) ?? [];
  const outgoing = children.get(focus) ?? [];
  const groups = [
    {role: 'parents', ids: incoming.map(edge => edge.parent_id)},
    {role: 'focus', ids: [focus]},
    {role: 'children', ids: outgoing.map(edge => edge.child_id)},
  ].filter(group => group.ids.length);

  const top = 44;
  const pitch = nodeHeight + rowGap;
  const maxRows = Math.max(...groups.map(group => group.ids.length));
  const centerRow = Math.min(1.5, (maxRows - 1) / 2);
  const positions = new Map();
  const columns = groups.map(({role, ids}, index) => {
    const x = padding + index * (nodeWidth + columnGap);
    const firstRow = Math.max(0, centerRow - (ids.length - 1) / 2);
    ids.forEach((id, row) => positions.set(id, {x, y: top + (firstRow + row) * pitch}));
    return {role, x};
  });

  return {
    positions, links: [...incoming, ...outgoing], columns, nodeWidth, nodeHeight,
    width: columns.at(-1).x + nodeWidth + padding,
    height: top + (maxRows - 1) * pitch + nodeHeight + padding,
  };
}

export function layoutDag({roots, children, parents, expanded}) {
  const visible = new Set(roots);
  for (const id of visible) {
    if (!expanded.has(id)) continue;
    for (const {child_id} of children.get(id) ?? []) visible.add(child_id);
  }

  const incoming = new Map([...visible].map(id => [id, []]));
  const outgoing = new Map([...visible].map(id => [id, []]));
  const links = [];
  for (const id of visible) {
    for (const edge of parents.get(id) ?? []) {
      if (!visible.has(edge.parent_id)) continue;
      incoming.get(id).push(edge.parent_id);
      outgoing.get(edge.parent_id).push(id);
      links.push(edge);
    }
  }

  // Every visible parent must be to the left of its child, even across branches.
  const remaining = new Map([...visible].map(id => [id, incoming.get(id).length]));
  const ready = [...visible].filter(id => remaining.get(id) === 0);
  const rank = new Map(ready.map(id => [id, 0]));
  for (let index = 0; index < ready.length; index++) {
    const id = ready[index];
    for (const child of outgoing.get(id)) {
      rank.set(child, Math.max(rank.get(child) ?? 0, rank.get(id) + 1));
      remaining.set(child, remaining.get(child) - 1);
      if (remaining.get(child) === 0) ready.push(child);
    }
  }
  if (ready.length !== visible.size) throw new Error('The visible ontology graph contains a cycle.');

  const positions = new Map();
  const grouped = new Set();
  let bandTop = padding;
  let width = 2 * padding;
  for (const id of visible) {
    if (grouped.has(id)) continue;
    const component = new Set([id]);
    const columns = [];
    for (const member of component) {
      grouped.add(member);
      const depth = rank.get(member);
      (columns[depth] ??= []).push(member);
      for (const neighbor of [...incoming.get(member), ...outgoing.get(member)]) component.add(neighbor);
    }

    let rowCount = 0;
    for (const [depth, column] of columns.entries()) {
      // Keep siblings near their parents and shared children between their parent branches.
      const centers = new Map(column.map(member => {
        const upstream = incoming.get(member);
        const total = upstream.reduce((sum, parent) => sum + positions.get(parent).y, 0);
        return [member, upstream.length ? total / upstream.length : 0];
      }));
      column.sort((first, second) => centers.get(first) - centers.get(second));
      const x = padding + depth * (nodeWidth + columnGap);
      for (const [row, member] of column.entries()) {
        positions.set(member, {x, y: bandTop + row * (nodeHeight + rowGap)});
      }
      rowCount = Math.max(rowCount, column.length);
      width = Math.max(width, x + nodeWidth + padding);
    }
    bandTop += rowCount * (nodeHeight + rowGap);
  }

  const height = bandTop + padding - (visible.size ? rowGap : 0);
  return {positions, links, width, height, nodeWidth, nodeHeight};
}
