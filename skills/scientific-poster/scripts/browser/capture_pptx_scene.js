() => {
  const root = document.querySelector(__POSTER_ROOT_SELECTOR__);
  if (!root) throw new Error('poster root was not found');
  const rootRect = root.getBoundingClientRect();
  const pageStyle = getComputedStyle(root);
  const pxPerInch = 96;
  const cleanId = (value) => String(value || 'poster')
    .trim().replace(/[^A-Za-z0-9._:-]+/g, '-').replace(/^-+|-+$/g, '') || 'poster';
  const visible = (element, requireBox = true) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || '1') > 0.02 &&
      (!requireBox || (rect.width > 1 && rect.height > 1));
  };
  const rgb = (value, fallback = '111111') => {
    const text = String(value || '');
    const match = text.match(/rgba?\((\d+)[, ]+(\d+)[, ]+(\d+)/i);
    const srgb = text.match(
      /color\(srgb\s+([0-9.]+%?)\s+([0-9.]+%?)\s+([0-9.]+%?)(?:\s*\/\s*([0-9.]+%?))?\)/i
    );
    const alphaValue = (part) => {
      if (!part) return 1;
      const value = Number.parseFloat(part);
      return Math.max(0, Math.min(1, part.endsWith('%') ? value / 100 : value));
    };
    const encode = (channels, alpha = 1) => {
      const fallbackHex = /^[0-9a-f]{6}$/i.test(String(fallback)) ?
        String(fallback) : '111111';
      const backdrop = [0, 2, 4].map(
        (index) => Number.parseInt(fallbackHex.slice(index, index + 2), 16)
      );
      return channels.map((channel, index) => Math.round(
        channel * alpha + backdrop[index] * (1 - alpha)
      ).toString(16).padStart(2, '0')).join('').toUpperCase();
    };
    if (srgb) {
      const channels = srgb.slice(1, 4).map((part) => {
        const channel = part.endsWith('%') ? Number.parseFloat(part) * 2.55 :
          Number.parseFloat(part) * 255;
        return Math.max(0, Math.min(255, channel));
      });
      return encode(channels, alphaValue(srgb[4]));
    }
    if (!match) {
      const hex = text.replace('#', '').trim();
      return /^[0-9a-f]{6}$/i.test(hex) ? hex.toUpperCase() : fallback;
    }
    const rgbaAlpha = text.match(/^rgba\([^)]*,\s*([0-9.]+%?)\s*\)$/i) ||
      text.match(/\/\s*([0-9.]+%?)\s*\)$/i);
    return encode(match.slice(1, 4).map(Number), alphaValue(rgbaAlpha?.[1]));
  };
  const platformFont = (source) => {
    const fonts = globalThis.__posterPlatformFonts;
    const nodes = [];
    if (source.nodeType === Node.TEXT_NODE) nodes.push(source);
    else {
      const walker = document.createTreeWalker(source, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node);
    }
    let face = null;
    for (const node of nodes) {
      if (!node.textContent.trim() || node.parentElement.closest('svg, math, style, script')) continue;
      const used = fonts?.get(node);
      if (!used) return null;
      for (const current of used) {
        if (face && face !== current) return null;
        face = current;
      }
    }
    return face;
  };
  const fontFace = (element, source = element) => platformFont(source) ||
    getComputedStyle(element).fontFamily.split(',')[0].replace(/["']/g, '').trim() || 'Arial';
  const textSpacing = (style) => {
    const lineHeight = Number.parseFloat(style.lineHeight);
    const letterSpacing = Number.parseFloat(style.letterSpacing) || 0;
    return {
      ...(Number.isFinite(lineHeight) ? {line_height_pt: lineHeight * 0.75} : {}),
      ...(letterSpacing ? {letter_spacing_pt: letterSpacing * 0.75} : {}),
    };
  };
  const textLineCount = (element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const tops = [];
    for (const box of range.getClientRects()) {
      if (box.width <= 0.5 || box.height <= 0.5) continue;
      if (!tops.some((top) => Math.abs(top - box.top) < 1)) tops.push(box.top);
    }
    return Math.max(1, tops.length);
  };
  const listMarkers = globalThis.__posterListMarkers || new Map();
  const hasListMarkers = (element) => [...listMarkers.keys()].some(
    (owner) => element === owner || element.contains(owner)
  );
  const contentBox = (element) => {
    const style = getComputedStyle(element);
    const bounds = element.getBoundingClientRect();
    const left = Number.parseFloat(style.borderLeftWidth || '0') +
      Number.parseFloat(style.paddingLeft || '0');
    const right = Number.parseFloat(style.borderRightWidth || '0') +
      Number.parseFloat(style.paddingRight || '0');
    const top = Number.parseFloat(style.borderTopWidth || '0') +
      Number.parseFloat(style.paddingTop || '0');
    const bottom = Number.parseFloat(style.borderBottomWidth || '0') +
      Number.parseFloat(style.paddingBottom || '0');
    return {
      left: bounds.left + left,
      top: bounds.top + top,
      width: Math.max(1, bounds.width - left - right),
      height: Math.max(1, bounds.height - top - bottom),
    };
  };
  const transparent = (value) => /rgba\([^)]*,\s*0(?:\.0+)?\)/i.test(String(value)) || value === 'transparent';
  const pageBackground = transparent(pageStyle.backgroundColor) ?
    rgb(getComputedStyle(document.body).backgroundColor, 'FFFFFF') :
    rgb(pageStyle.backgroundColor, 'FFFFFF');
  const backdropColor = (element) => {
    const layers = [];
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      const color = getComputedStyle(parent).backgroundColor;
      if (!transparent(color)) layers.push(color);
      if (parent === root) break;
    }
    return layers.reverse().reduce(
      (backdrop, color) => rgb(color, backdrop),
      pageBackground
    );
  };
  const rect = (element) => {
    const box = element.getBoundingClientRect();
    return {
      x: Math.max(0, (box.left - rootRect.left) / pxPerInch),
      y: Math.max(0, (box.top - rootRect.top) / pxPerInch),
      w: box.width / pxPerInch,
      h: box.height / pxPerInch,
    };
  };
  const textRect = (element) => {
    const box = contentBox(element);
    return {
      x: Math.max(0, (box.left - rootRect.left) / pxPerInch),
      y: Math.max(0, (box.top - rootRect.top) / pxPerInch),
      w: box.width / pxPerInch,
      h: box.height / pxPerInch,
    };
  };
  const collapsedTableBox = (element) => {
    const style = getComputedStyle(element);
    return ['table', 'inline-table'].includes(style.display) && style.borderCollapse === 'collapse';
  };
  const tableGrid = (table) => {
    // Physical tracks retain independent row groups and actual spacing. Chromium
    // has already resolved RTL, spans and CSS layout; do not lay out a second grid.
    const tableRows = [...table.rows];
    const cells = tableRows.flatMap((row) => [...row.cells].filter((cell) => visible(cell))
      .map((cell) => ({cell, box: cell.getBoundingClientRect()})));
    const boxes = [...cells.map(({box}) => box), ...tableRows.map((row) => row.getBoundingClientRect())
      .filter((box) => box.width > 0 && box.height > 0)];
    const xs = [...new Set(boxes.flatMap((box) => [box.left, box.right]))].sort((a, b) => a - b);
    const ys = [...new Set(boxes.flatMap((box) => [box.top, box.bottom]))].sort((a, b) => a - b);
    const grid = ys.slice(1).map(() => Array(xs.length - 1).fill(null));
    for (const entry of cells) {
      entry.row = ys.indexOf(entry.box.top);
      entry.column = xs.indexOf(entry.box.left);
      entry.row_span = ys.indexOf(entry.box.bottom) - entry.row;
      entry.column_span = xs.indexOf(entry.box.right) - entry.column;
      for (let r = entry.row; r < entry.row + entry.row_span; r += 1) {
        for (let c = entry.column; c < entry.column + entry.column_span; c += 1) grid[r][c] = entry;
      }
    }
    return {
      grid, cells, xs, ys,
      bounds: {
        x: (xs[0] - rootRect.left) / pxPerInch, y: (ys[0] - rootRect.top) / pxPerInch,
        w: (xs.at(-1) - xs[0]) / pxPerInch, h: (ys.at(-1) - ys[0]) / pxPerInch,
      },
      columnWidths: xs.slice(1).map((edge, index) => (edge - xs[index]) / pxPerInch),
      rowHeights: ys.slice(1).map((edge, index) => (edge - ys[index]) / pxPerInch),
    };
  };
  const tableCellBorders = (table, grid, row, column, fill, xs, ys) => {
    const entry = grid[row][column];
    const collapsed = getComputedStyle(table).borderCollapse === 'collapse';
    const parts = [...table.rows, table.tHead, ...table.tBodies, table.tFoot,
      ...(collapsedTableBox(table) ? [table] : [])].filter(Boolean);
    return Object.fromEntries([
      ['top', -1, 0, 'bottom'], ['right', 0, 1, 'left'],
      ['bottom', 1, 0, 'top'], ['left', 0, -1, 'right'],
    ].map(([side, rowOffset, columnOffset, opposite]) => {
      const neighbor = grid[row + rowOffset]?.[column + columnOffset];
      if (entry && neighbor === entry) return [side, {width_pt: 0, color: fill}];
      const boundary = side === 'left' ? xs[column] : side === 'right' ? xs[column + 1] :
        side === 'top' ? ys[row] : ys[row + 1];
      const middle = rowOffset ? (xs[column] + xs[column + 1]) / 2 : (ys[row] + ys[row + 1]) / 2;
      const candidates = entry ? [[entry.cell, side]] : [];
      if (collapsed) {
        if (neighbor) candidates.push([neighbor.cell, opposite]);
        // A row rule can meet a cell which began in an earlier row. Use the
        // actual rule boundary, including across spans, never a logical row index.
        for (const part of parts) {
          const box = part === table ?
            {left: xs[0], right: xs.at(-1), top: ys[0], bottom: ys.at(-1)} : part.getBoundingClientRect();
          const start = rowOffset ? box.left : box.top;
          const end = rowOffset ? box.right : box.bottom;
          if (middle < start || middle > end) continue;
          for (const edge of [side, opposite]) {
            if (Math.abs(box[edge] - boundary) < 0.02) candidates.push([part, edge]);
          }
        }
      }
      const borders = candidates.map(([source, edge]) => {
        const style = getComputedStyle(source);
        return {
          width_pt: Number.parseFloat(style.getPropertyValue(`border-${edge}-width`) || '0') * 0.75,
          color: rgb(style.getPropertyValue(`border-${edge}-color`), fill),
        };
      });
      return [side, borders.reduce((best, border) => border.width_pt > best.width_pt ? border : best,
        {width_pt: 0, color: fill})];
    }));
  };
  const moduleId = (element) => element.closest('[data-poster-module]')?.getAttribute('data-poster-module') || null;
  const baseId = (element) => cleanId(
    element.getAttribute('data-poster-id') ||
    element.closest('[data-poster-id]')?.getAttribute('data-poster-id') ||
    moduleId(element) || 'poster'
  );
  const counters = new Map();
  const objectId = (element, role) => {
    const key = `${baseId(element)}.${role}`;
    const next = (counters.get(key) || 0) + 1;
    counters.set(key, next);
    return `${key}.${next}`;
  };
  const objects = [];
  const compositeElements = [...root.querySelectorAll('table')].filter(
    (element) => visible(element) && [...element.rows].some(
      (row) => [...row.cells].some((cell) => visible(cell))
    )
  );
  const compositeIds = new Map(
    compositeElements.map((element) => [element, objectId(element, 'table')])
  );
  const containerBinding = (element) => {
    const container = element.parentElement?.closest('table');
    const containerId = container ? compositeIds.get(container) : null;
    return containerId ? {container_id: containerId} : {};
  };
  const appendBoxVisual = (element, role, paintBorders = true) => {
    const style = getComputedStyle(element);
    const bounds = rect(element);
    const hasFill = !transparent(style.backgroundColor);
    const outlineWidth = Number.parseFloat(style.outlineWidth || '0');
    const outlineStyle = String(style.outlineStyle || 'none').toLowerCase();
    const hasOutline = outlineWidth > 0.5 &&
      !['none', 'hidden'].includes(outlineStyle) && !transparent(style.outlineColor);
    const outlineOffset = Number.parseFloat(style.outlineOffset || '0');
    const sides = [
      ['top', style.borderTopWidth, style.borderTopColor, style.borderTopStyle],
      ['right', style.borderRightWidth, style.borderRightColor, style.borderRightStyle],
      ['bottom', style.borderBottomWidth, style.borderBottomColor, style.borderBottomStyle],
      ['left', style.borderLeftWidth, style.borderLeftColor, style.borderLeftStyle],
    ].map(([name, width, color, pattern]) => ({
      name,
      width: paintBorders ? Number.parseFloat(width || '0') : 0,
      color, pattern,
    }));
    const activeBorders = sides.filter(
      (side) => side.width > 0.5 && !transparent(side.color)
    );
    if (!hasFill && activeBorders.length === 0 && !hasOutline) return;
    const backdrop = backdropColor(element);
    const fill = transparent(style.backgroundColor) ? backdrop :
      rgb(style.backgroundColor, backdrop);
    const firstBorder = activeBorders[0];
    const uniformOutline = activeBorders.length === 4 && activeBorders.every(
      (side) => Math.abs(side.width - firstBorder.width) < 0.05 &&
        rgb(side.color) === rgb(firstBorder.color)
    );
    const outlineOnBounds = hasOutline && !uniformOutline &&
      Math.abs(outlineOffset + outlineWidth / 2) < 0.05;
    const radius = Number.parseFloat(style.borderRadius || '0') / pxPerInch;
    if (hasFill || uniformOutline || outlineOnBounds) {
      const frameColor = uniformOutline ? firstBorder.color :
        outlineOnBounds ? style.outlineColor : backdrop;
      const frameWidth = uniformOutline ? firstBorder.width :
        outlineOnBounds ? outlineWidth : 0;
      objects.push({
        id: objectId(element, role), kind: 'shape', role,
        module_id: moduleId(element), ...bounds,
        fill,
        fill_enabled: hasFill,
        line: rgb(frameColor, backdrop),
        line_width_pt: frameWidth * 72 / pxPerInch,
        radius,
      });
    }
    // The solid border ring preserves curved single/unequal edges without covering
    // its transparent center. Other paint styles retain their existing path.
    const roundedSolidBorder = radius > 0 && /^\d+(?:\.\d+)?px$/.test(style.borderRadius) &&
      activeBorders.length > 0 && sides.filter((side) => side.width > 0).every(
        (side) => side.pattern === 'solid' && !transparent(side.color) &&
          rgb(side.color, backdrop) === rgb(firstBorder.color, backdrop)
      );
    if (!uniformOutline && roundedSolidBorder) {
      const color = rgb(firstBorder.color, backdrop);
      objects.push({
        id: objectId(element, `${role}-border`), kind: 'shape', role: `${role}-border`,
        module_id: moduleId(element), ...bounds, fill: color, fill_enabled: true,
        line: color, line_width_pt: 0,
        radius: Math.min(radius, bounds.w / 2, bounds.h / 2),
        border_widths: Object.fromEntries(sides.map((side) => [side.name, side.width / pxPerInch])),
      });
    } else if (!uniformOutline) {
      for (const border of activeBorders) {
        const thickness = border.width / pxPerInch;
        const geometry = border.name === 'top' ?
          {x: bounds.x, y: bounds.y, w: bounds.w, h: thickness} :
          border.name === 'right' ?
            {x: bounds.x + bounds.w - thickness, y: bounds.y, w: thickness, h: bounds.h} :
            border.name === 'bottom' ?
              {x: bounds.x, y: bounds.y + bounds.h - thickness, w: bounds.w, h: thickness} :
              {x: bounds.x, y: bounds.y, w: thickness, h: bounds.h};
        const color = rgb(border.color, backdrop);
        objects.push({
          id: objectId(element, `${role}-border-${border.name}`),
          kind: 'shape', role: `${role}-border`, module_id: moduleId(element),
          ...geometry, fill: color, fill_enabled: true,
          line: color, line_width_pt: 0, radius: 0,
        });
      }
    }
    if (hasOutline && !outlineOnBounds) {
      const outset = (outlineOffset + outlineWidth / 2) / pxPerInch;
      const pageWidth = rootRect.width / pxPerInch;
      const pageHeight = rootRect.height / pxPerInch;
      const left = Math.max(0, bounds.x - outset);
      const top = Math.max(0, bounds.y - outset);
      const right = Math.min(pageWidth, bounds.x + bounds.w + outset);
      const bottom = Math.min(pageHeight, bounds.y + bounds.h + outset);
      const color = rgb(style.outlineColor, backdrop);
      objects.push({
        id: objectId(element, `${role}-outline`),
        kind: 'shape', role: `${role}-outline`, module_id: moduleId(element),
        x: left, y: top, w: right - left, h: bottom - top,
        fill: pageBackground, fill_enabled: false,
        line: color, line_width_pt: outlineWidth * 72 / pxPerInch, radius,
      });
    }
  };
  // Capture box paint once in DOM order: a shared surface precedes its children,
  // regardless of its HTML tag. Table cells and SVG screenshots carry their own paint;
  // Office Math carries the expression, not the math element's CSS background/border.
  for (const element of root.querySelectorAll('*')) {
    const screenshotOwnsPaint = element.matches('img') &&
      (element.currentSrc || element.getAttribute('src') || '').startsWith('data:image/svg+xml');
    const table = element.closest('table');
    if (element.closest('svg') || (table && table !== element) || element.parentElement?.closest('math') ||
        screenshotOwnsPaint || !visible(element)) continue;
    const role = element.hasAttribute('data-poster-module') ? 'module-frame' :
      element.hasAttribute('data-poster-title-band') ? 'title-band' : 'decoration';
    const firstObject = objects.length;
    appendBoxVisual(element, role, !(compositeIds.has(element) && collapsedTableBox(element)));
    if (compositeIds.has(element)) {
      for (const item of objects.slice(firstObject)) item.container_id = compositeIds.get(element);
    }
  }

  const images = [...root.querySelectorAll('img, svg')];
  for (const [imageIndex, element] of images.entries()) {
    if (!visible(element)) continue;
    let src = '';
    let alt = '';
    if (element.tagName.toLowerCase() === 'svg') {
      const xml = new XMLSerializer().serializeToString(element);
      src = `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(xml)))}`;
      alt = element.getAttribute('aria-label') || 'Inline scientific figure';
    } else {
      src = element.currentSrc || element.getAttribute('src') || '';
      alt = element.getAttribute('alt') || '';
    }
    if (!src) continue;
    objects.push({
      id: objectId(element, 'figure'), kind: 'image', role: 'figure',
      module_id: moduleId(element), ...rect(element), src, alt,
      image_index: imageIndex,
    });
  }

  const tables = compositeElements;
  const measuredCells = new Set([...root.querySelectorAll('th,td')].filter(
    (cell) => cell.querySelector('math, table') || hasListMarkers(cell)
  ));
  const tableCellRuns = (cell, text, fill) => {
    const selection = getSelection();
    const range = document.createRange();
    range.setStart(cell, 0);
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    const runs = [];
    let previous = '';
    try {
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const owner = node.parentElement;
        if (!visible(owner, false) || owner.closest('math, svg, style, script') ||
            owner.closest('table') !== cell.closest('table')) continue;
        range.setEnd(node, node.textContent.length);
        selection.removeAllRanges();
        selection.addRange(range);
        const prefix = selection.toString().trimStart();
        if (!prefix.startsWith(previous)) return [];
        const value = prefix.slice(previous.length);
        previous = prefix;
        if (!value) continue;
        const style = getComputedStyle(owner);
        runs.push({
          text: value,
          font_size_pt: Number.parseFloat(style.fontSize || '16') * 0.75,
          ...textSpacing(style),
          font_face: fontFace(owner, node), color: rgb(style.color, fill),
          bold: Number.parseInt(style.fontWeight || '400', 10) >= 600,
          italic: style.fontStyle === 'italic',
        });
      }
    } finally {
      selection.removeAllRanges();
    }
    while (runs.length && previous.length > text.length) {
      const extra = previous.length - text.length;
      const removed = Math.min(extra, runs.at(-1).text.length);
      runs.at(-1).text = runs.at(-1).text.slice(0, -removed);
      previous = previous.slice(0, -removed);
      if (!runs.at(-1).text) runs.pop();
    }
    return previous === text ? runs : [];
  };
  for (const element of tables) {
    const {grid, cells, xs, ys, bounds, columnWidths, rowHeights} = tableGrid(element);
    // Mixed-content cells keep native table geometry and browser-measured text/math.
    const rows = grid.map((row, r) => row.map((entry, c) =>
      entry && entry.row === r && entry.column === c && !measuredCells.has(entry.cell) ?
        entry.cell.innerText.trim() : ''
    ));
    const stylesByCell = new Map(cells.map((entry) => {
      const {cell} = entry;
      const style = getComputedStyle(cell);
      const face = fontFace(cell);
      const wordWrap = textLineCount(cell) > 1;
      const fill = rgb(style.backgroundColor, backdropColor(cell));
      const align = ['start', 'end'].includes(style.textAlign) ?
        ((style.textAlign === 'start') === (style.direction === 'rtl') ? 'right' : 'left') :
        style.textAlign;
      return [entry, {
        font_size_pt: Number.parseFloat(style.fontSize || '16') * 0.75,
        ...textSpacing(style),
        font_face: face, color: rgb(style.color, fill), fill,
        bold: Number.parseInt(style.fontWeight || '400', 10) >= 600,
        italic: style.fontStyle === 'italic',
        align: ['center', 'right'].includes(align) ? align : 'left',
        word_wrap: wordWrap,
        vertical_align: ['middle', 'bottom'].includes(style.verticalAlign) ? style.verticalAlign : 'top',
        padding: Object.fromEntries(['top', 'right', 'bottom', 'left'].map((side, index) => [
          side, globalThis.__posterCellInsets.get(cell)[index] / pxPerInch,
        ])),
      }];
    }));
    const cellStyles = grid.map((row, r) => row.map((entry, c) => {
      const style = stylesByCell.get(entry) || {
        font_size_pt: 1, padding: {top: 0, right: 0, bottom: 0, left: 0},
        fill: rgb(getComputedStyle(element).backgroundColor, backdropColor(element)),
      };
      return {...style, borders: tableCellBorders(element, grid, r, c, style.fill, xs, ys)};
    }));
    const cellRuns = grid.map((row, r) => row.map((entry, c) => {
      if (!entry || entry.row !== r || entry.column !== c || measuredCells.has(entry.cell)) return [];
      const base = stylesByCell.get(entry);
      const runs = tableCellRuns(entry.cell, rows[r][c], base.fill);
      const plain = runs.length === 1 && ['font_size_pt', 'font_face', 'color', 'bold', 'italic',
        'line_height_pt', 'letter_spacing_pt'].every((field) => runs[0][field] === base[field]);
      return plain ? [] : runs;
    }));
    objects.push({
      id: compositeIds.get(element), kind: 'table', role: 'evidence',
      module_id: moduleId(element), ...containerBinding(element), ...bounds, rows,
      row_heights: rowHeights, column_widths: columnWidths,
      cell_styles: cellStyles, cell_runs: cellRuns,
      merges: cells.filter((entry) => entry.row_span > 1 || entry.column_span > 1)
        .map(({row, column, row_span, column_span}) => ({row, column, row_span, column_span})),
    });
    // Native merged-cell borders cannot reliably express a partial outer edge.
    // Keep visible ink as editable strokes, bound to the same native table.
    const collapsed = getComputedStyle(element).borderCollapse === 'collapse';
    const borderGrid = cellStyles.map((row) => row.map((style) => style.borders));
    const verticalJoin = (row, column) => Math.max(...[row - 1, row].map((r) =>
      (column ? borderGrid[r]?.[column - 1]?.right : borderGrid[r]?.[0]?.left)?.width_pt || 0
    )) / 144;
    for (const [r, row] of cellStyles.entries()) {
      for (const [c, style] of row.entries()) {
        const borders = style.borders;
        style.borders = {};
        for (const [side, border] of Object.entries(borders)) {
          if (!border.width_pt) continue;
          if (collapsed && ((side === 'top' && r > 0) ||
              (side === 'left' && c > 0))) continue;
          const thickness = border.width_pt / 72;
          const x = (xs[c] - rootRect.left) / pxPerInch, y = (ys[r] - rootRect.top) / pxPerInch;
          const w = (xs[c + 1] - xs[c]) / pxPerInch, h = (ys[r + 1] - ys[r]) / pxPerInch;
          const inset = collapsed ? -thickness / 2 : 0;
          const boundaryRow = r + (side === 'bottom' ? 1 : 0);
          const left = collapsed ? verticalJoin(boundaryRow, c) : 0;
          const right = collapsed ? verticalJoin(boundaryRow, c + 1) : 0;
          const geometry = side === 'top' ? {x: x - left, y: y + inset, w: w + left + right, h: thickness} :
            side === 'bottom' ? {x: x - left, y: y + h - thickness - inset, w: w + left + right, h: thickness} :
            side === 'left' ? {x: x + inset, y, w: thickness, h} :
            {x: x + w - thickness - inset, y, w: thickness, h};
          objects.push({
            id: objectId(element, 'table-border'), kind: 'shape', role: 'table-border',
            module_id: moduleId(element), container_id: compositeIds.get(element),
            ...geometry, fill: border.color, fill_enabled: true,
            line: border.color, line_width_pt: 0, radius: 0,
          });
        }
      }
    }
  }

  // Keep each MathML expression intact for native PowerPoint Office Math export.
  const mathElements = [...root.querySelectorAll('math')];
  const mathObjects = [];
  for (const math of mathElements) {
    if (!visible(math)) continue;
    const style = getComputedStyle(math);
    // Display math can occupy the whole column while its expression is much narrower.
    // Office needs the browser-laid-out expression bounds, not that layout container.
    const content = document.createRange();
    content.selectNodeContents(math);
    mathObjects.push({
      id: objectId(math, 'native-equation'), kind: 'equation', role: 'equation',
      module_id: moduleId(math), ...containerBinding(math), ...rect(content),
      mathml: new XMLSerializer().serializeToString(math),
      latex: math.getAttribute('data-latex') || '',
      font_size_pt: Number.parseFloat(style.fontSize || '16') * 0.75,
      font_face: 'Cambria Math', color: rgb(style.color, '111111'),
      align: ['left', 'right'].includes(style.textAlign) ? style.textAlign : 'center',
    });
  }

  const explicitText = 'h1, h2, h3, h4, p, li, figcaption, [data-poster-text], [role="rowheader"]';
  const hasDirectText = (element) => [...element.childNodes].some(
    (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim()
  );
  const textCandidates = [
    ...new Set([
      ...root.querySelectorAll(explicitText),
      ...measuredCells,
      ...[root, ...root.querySelectorAll('*')].filter(hasDirectText),
    ])
  ].filter((element) => {
    if (element.closest('table')) return measuredCells.has(element);
    return !element.closest('math, svg, style, script') &&
      !element.matches('[data-content-role="equation"]');
  });
  const textCandidateSet = new Set(textCandidates);
  const textElements = textCandidates.filter((element) => {
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      if (textCandidateSet.has(parent)) return false;
    }
    return true;
  });
  const needsTextFragments = (element) => {
    // Text can paint outside a zero-size container or under display:contents.
    // Its Range geometry, not that container's box, owns the native text bounds.
    if (!visible(element) || element.querySelector('math') || hasListMarkers(element)) return true;
    const inherited = getComputedStyle(element);
    // Direct text is an anonymous flex/grid item, not a paragraph at the container origin.
    if (['flex', 'inline-flex', 'grid', 'inline-grid'].includes(inherited.display)) return true;
    const properties = ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'color', 'textTransform', 'letterSpacing'];
    return [...element.querySelectorAll('*')].some((child) => {
      if (child.tagName.toLowerCase() === 'br') return true;
      if (!visible(child)) return false;
      const style = getComputedStyle(child);
      return /^(block|flow-root|grid|flex|list-item|table)/.test(style.display) ||
        properties.some((property) => style[property] !== inherited[property]);
    });
  };
  const textFragments = (element) => {
    // Reuse native text objects for painted, uniform-style lines; no rich-text schema.
    const fragments = [];
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const owner = node.parentElement;
      if (!visible(owner, false) || owner.closest('math, svg, style, script')) continue;
      if (owner.closest('table') && !measuredCells.has(owner.closest('th, td'))) continue;
      const style = getComputedStyle(owner);
      const fontSize = Number.parseFloat(style.fontSize || '16');
      const lineHeight = Number.parseFloat(style.lineHeight) || fontSize * 1.2;
      const range = document.createRange();
      let offset = 0;
      let fragment = null;
      for (const character of node.textContent) {
        range.setStart(node, offset);
        offset += character.length;
        range.setEnd(node, offset);
        const box = range.getBoundingClientRect();
        if (box.width < 0.1 || box.height < 0.1) {
          if (fragment) fragment.range.setEnd(node, offset);
          continue;
        }
        if (/\s/.test(character)) continue;
        if (!fragment || Math.abs(fragment.lineTop - box.top) >= 1) {
          fragment = {
            owner, node, range: range.cloneRange(), lineTop: box.top,
            bounds: {
              left: box.left,
              top: box.top - Math.max(0, lineHeight - box.height) / 2,
              width: box.width,
              height: Math.max(lineHeight, box.height),
            },
          };
          fragments.push(fragment);
        } else {
          fragment.range.setEnd(node, offset);
          const right = Math.max(fragment.bounds.left + fragment.bounds.width, box.right);
          fragment.bounds.left = Math.min(fragment.bounds.left, box.left);
          fragment.bounds.width = right - fragment.bounds.left;
        }
      }
    }
    // Selection reads the browser's contextual transformations; Range.toString does not.
    const selection = getSelection();
    try {
      for (const fragment of fragments) {
        selection.removeAllRanges();
        selection.addRange(fragment.range);
        fragment.text = selection.toString();
      }
    } finally {
      selection.removeAllRanges();
    }
    return fragments;
  };
  for (const element of textElements) {
    if (!visible(element, false)) continue;
    let text = element.innerText.replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const fragments = needsTextFragments(element) ? textFragments(element) :
      [{owner: element, text, bounds: null}];
    for (const fragment of fragments) {
      // A container's direct text may share a subtree with headings and modules.
      // Preserve each painted run's semantic owner, not the traversal root's role.
      const owner = fragment.owner;
      const heading = owner.closest('h1, h2, h3, h4');
      const role = heading ? (heading.tagName.toLowerCase() === 'h1' ? 'title' : 'heading') :
        owner.closest('figcaption,[data-content-role="caption"],.fig-caption') ? 'caption' : 'body';
      const style = getComputedStyle(fragment.owner);
      const fontSource = fragment.node || fragment.owner;
      const fontFaceValue = fontFace(fragment.owner, fontSource);
      const wordWrap = !fragment.bounds && textLineCount(element) > 1;
      const fontSizePx = Number.parseFloat(style.fontSize || '16');
      const fontPreflighted = Boolean(platformFont(fontSource));
      const bounds = fragment.bounds ? {
        x: Math.max(0, (fragment.bounds.left - rootRect.left) / pxPerInch),
        y: Math.max(0, (fragment.bounds.top - rootRect.top) / pxPerInch),
        w: fragment.bounds.width / pxPerInch, h: fragment.bounds.height / pxPerInch,
      } : textRect(element);
      objects.push({
        id: objectId(owner, role), kind: 'text', role,
        module_id: moduleId(owner), ...containerBinding(owner), ...bounds,
        text: fragment.text,
        font_size_pt: fontSizePx * 0.75,
        font_face: fontFaceValue,
        ...textSpacing(style),
        color: rgb(style.color, '111111'),
        bold: Number.parseInt(style.fontWeight || '400', 10) >= 600,
        italic: style.fontStyle === 'italic',
        align: !fragment.bounds && ['center', 'right'].includes(style.textAlign) ? style.textAlign : 'left',
        // Preserve authored typography when Chromium confirmed the rendered face.
        fit: fontPreflighted ? 'none' : 'shrink', word_wrap: wordWrap,
        font_preflighted: fontPreflighted,
      });
    }
  }
  for (const [element, {runs, faces}] of listMarkers) {
    if (!root.contains(element) || !visible(element)) continue;
    const style = getComputedStyle(element, '::marker');
    const ownerBounds = element.getBoundingClientRect();
    const backdrop = backdropColor(element);
    const face = faces.length === 1 ? faces[0] :
      style.fontFamily.split(',')[0].replace(/["']/g, '').trim();
    for (const {text, bounds: [x, y, w, h]} of runs) {
      if (!text.trim() || w <= 0 || h <= 0) continue;
      // Outside markers do not paint over the list item's own background.
      const inside = x + w / 2 >= ownerBounds.left && x + w / 2 <= ownerBounds.right &&
        y + h / 2 >= ownerBounds.top && y + h / 2 <= ownerBounds.bottom;
      const background = inside ?
        rgb(getComputedStyle(element).backgroundColor, backdrop) : backdrop;
      objects.push({
        id: objectId(element, 'list-marker'), kind: 'text', role: 'list-marker',
        module_id: moduleId(element), ...containerBinding(element),
        x: Math.max(0, (x - rootRect.left) / pxPerInch),
        y: Math.max(0, (y - rootRect.top) / pxPerInch), w: w / pxPerInch, h: h / pxPerInch,
        text, font_size_pt: Number.parseFloat(style.fontSize) * 0.75, font_face: face,
        color: rgb(style.color, background),
        bold: Number.parseInt(style.fontWeight, 10) >= 600, italic: style.fontStyle === 'italic',
        letter_spacing_pt: Number.parseFloat(style.letterSpacing) * 0.75 || 0,
        align: style.direction === 'rtl' ? 'right' : 'left',
        fit: faces.length === 1 ? 'none' : 'shrink', word_wrap: false,
        font_preflighted: faces.length === 1,
      });
    }
  }
  // Text backgrounds must not paint over native math.
  objects.push(...mathObjects);
  return {
    schema: 'scientific-poster.pptx-scene.v3',
    page: {
      width_in: rootRect.width / pxPerInch,
      height_in: rootRect.height / pxPerInch,
      background: pageBackground
    },
    objects,
  };
}
