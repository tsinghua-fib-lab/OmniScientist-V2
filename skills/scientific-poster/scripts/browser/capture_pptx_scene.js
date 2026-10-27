() => {
  const root = document.querySelector(__POSTER_ROOT_SELECTOR__);
  if (!root) throw new Error('poster root was not found');
  const rootRect = root.getBoundingClientRect();
  const pageStyle = getComputedStyle(root);
  const pxPerInch = 96;
  const paintVisibility = (__PAINT_VISIBILITY__)(root);
  const nativeSvgs = new Set([...root.querySelectorAll('svg')].filter(
    element => !element.ownerSVGElement && !element.closest('[data-source-figure-sha256]')
  ));
  const nativeSvgIds = new Map();
  const svgPaintIndices = new WeakMap();
  const paintedImageSize = __PAINTED_IMAGE_SIZE__;
  const cleanId = (value) => {
    const allowed = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-';
    let result = '', invalidRun = false;
    for (const character of String(value || 'poster').trim()) {
      if (allowed.includes(character)) { result += character; invalidRun = false; }
      else if (!invalidRun) { result += '-'; invalidRun = true; }
    }
    while (result.startsWith('-')) result = result.slice(1);
    while (result.endsWith('-')) result = result.slice(0, -1);
    return result || 'poster';
  };
  const colorCanvas = document.createElement('canvas');
  colorCanvas.width = colorCanvas.height = 1;
  const colorContext = colorCanvas.getContext('2d', {willReadFrequently: true});
  const colorPixels = new Map();
  const colorPixel = (value) => {
    const text = String(value || '');
    if (!colorPixels.has(text)) {
      colorContext.clearRect(0, 0, 1, 1);
      colorContext.fillStyle = 'transparent';
      colorContext.fillStyle = text;
      colorContext.fillRect(0, 0, 1, 1);
      colorPixels.set(text, colorContext.getImageData(0, 0, 1, 1).data);
    }
    return colorPixels.get(text);
  };
  const visible = (element, requireBox = true) => {
    const rect = element.getBoundingClientRect();
    return paintVisibility.visible(element, '', false) &&
      (!requireBox || (rect.width > 1 && rect.height > 1));
  };
  const rgb = (value, fallback = '111111') => {
    // A null backdrop preserves the source channels for a native alpha fill.
    let pixel;
    if (fallback === null) pixel = colorPixel(value);
    else {
      colorContext.clearRect(0, 0, 1, 1);
      colorContext.fillStyle = `#${fallback}`;
      colorContext.fillRect(0, 0, 1, 1);
      colorContext.fillStyle = 'transparent';
      colorContext.fillStyle = String(value || '');
      colorContext.fillRect(0, 0, 1, 1);
      pixel = colorContext.getImageData(0, 0, 1, 1).data;
    }
    return [...pixel].slice(0, 3)
      .map((channel) => channel.toString(16).padStart(2, '0')).join('').toUpperCase();
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
      if (!node.textContent.trim() || node.parentElement.closest('math, style, script')) continue;
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
    getComputedStyle(element).fontFamily.split(',')[0].replaceAll('"', '').replaceAll("'", '').trim() || 'Arial';
  const selectionText = (range) => {
    const ancestor = range.commonAncestorContainer;
    const owner = ancestor.nodeType === Node.TEXT_NODE ? ancestor.parentElement : ancestor;
    const styles = [owner, ...owner.querySelectorAll('*')].map((element) => [element, element.getAttribute('style')]);
    const selection = getSelection();
    try {
      // Selection exposes Chromium's transformed text; user-select changes
      // interaction policy while preserving the measured layout.
      for (const [element] of styles) element.style.setProperty('user-select', 'text', 'important');
      selection.removeAllRanges();
      selection.addRange(range);
      return selection.toString();
    } finally {
      selection.removeAllRanges();
      for (const [element, style] of styles) {
        if (style === null) {
          element.getAttribute('style');
          element.removeAttribute('style');
        } else element.setAttribute('style', style);
      }
    }
  };
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
  const pseudoPaints = globalThis.__posterPseudoPaints || [];
  const hasListMarkers = (element) => [...listMarkers.keys()].some(
    (owner) => element === owner || element.contains(owner)
  );
  const hasPseudoPaint = (element) => pseudoPaints.some(
    ({owner}) => element === owner || element.contains(owner)
  );
  const transparent = (value) => colorPixel(value)[3] === 0;
  const pageBackground = transparent(pageStyle.backgroundColor) ?
    rgb(getComputedStyle(document.body).backgroundColor, 'FFFFFF') :
    rgb(pageStyle.backgroundColor, 'FFFFFF');
  const backdropColor = (element, includeSelf = false) => {
    const layers = [];
    for (let parent = includeSelf ? element : element.parentElement; parent; parent = parent.parentElement) {
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
  const paintOrder = globalThis.__posterPaintOrder || new WeakMap();
  const objectPaintOrder = new WeakMap();
  const transformError = (element, reason) => {
    throw new Error(`CSS native transform on ${baseId(element)} cannot be represented: ${reason}.`);
  };
  const transforms = new WeakMap();
  const styleTransform = (style, element) => {
    let matrix = new DOMMatrix();
    // Individual properties precede the transform list. Translation and origins
    // are already included in Chromium's measured box center.
    if (style.translate !== 'none' && style.translate.split(' ').length > 2) {
      transformError(element, '3D translation');
    }
    if (style.rotate !== 'none') {
      const parts = style.rotate.split(' ');
      const angle = parts.pop();
      if (parts.length && !(parts.length === 1 && parts[0] === 'z')) {
        transformError(element, '3D rotation');
      }
      matrix = matrix.rotate(CSSNumericValue.parse(angle).to('deg').value);
    }
    if (style.scale !== 'none') {
      const values = style.scale.split(' ').map(Number);
      if (values.length > 2) transformError(element, '3D scale');
      matrix = matrix.scale(values[0], values[1] ?? values[0]);
    }
    if (style.transform !== 'none') matrix = matrix.multiply(new DOMMatrix(style.transform));
    if (!matrix.is2D) transformError(element, '3D or perspective geometry');
    return new DOMMatrix([matrix.a, matrix.b, matrix.c, matrix.d, 0, 0]);
  };
  const elementTransform = (element) => {
    if (!element) return new DOMMatrix();
    if (!transforms.has(element)) {
      transforms.set(element, elementTransform(element.parentElement)
        .multiply(styleTransform(getComputedStyle(element), element)));
    }
    return transforms.get(element);
  };
  const boxTransform = (element, style, measured, pseudoPaint = null) => {
    const matrix = pseudoPaint ? elementTransform(element).multiply(styleTransform(style, element)) :
      elementTransform(element);
    if (matrix.isIdentity) return null;
    const sx = Math.hypot(matrix.a, matrix.b), sy = Math.hypot(matrix.c, matrix.d);
    if (sx === 0 || sy === 0 || Math.abs(matrix.a * matrix.c + matrix.b * matrix.d) > sx * sy * 1e-6) {
      transformError(element, 'skew or degenerate axes');
    }
    // Used CSS dimensions retain fractional pixels; offsetWidth rounds them.
    const width = Number.parseFloat(style.width) + (style.boxSizing === 'border-box' ? 0 :
      ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth']
        .reduce((sum, key) => sum + Number.parseFloat(style[key]), 0));
    const height = Number.parseFloat(style.height) + (style.boxSizing === 'border-box' ? 0 :
      ['paddingTop', 'paddingBottom', 'borderTopWidth', 'borderBottomWidth']
        .reduce((sum, key) => sum + Number.parseFloat(style[key]), 0));
    // A pseudo box has its own layout; its owner may span lines or use display:contents.
    // Resolved dimensions and transformed bounds below validate the pseudo's rectangle.
    if (!(width > 0 && height > 0) || (!pseudoPaint && element.getClientRects().length !== 1)) {
      transformError(element, 'paint requires one resolved CSS border box');
    }
    const expectedWidth = Math.abs(matrix.a) * width + Math.abs(matrix.c) * height;
    const expectedHeight = Math.abs(matrix.b) * width + Math.abs(matrix.d) * height;
    if (Math.abs(expectedWidth - measured.w * pxPerInch) > 0.05 ||
        Math.abs(expectedHeight - measured.h * pxPerInch) > 0.05) {
      transformError(element, 'CSS border box differs from the measured transformed bounds');
    }
    return {
      matrix, sx, sy, width: width / pxPerInch, height: height / pxPerInch,
      // An affine rectangle and its axis-aligned bounds share a center. Chromium
      // supplies layout, scrolling and transform-origin; Office supplies rotation.
      cx: measured.x + measured.w / 2, cy: measured.y + measured.h / 2,
      rotation: (Math.atan2(matrix.b, matrix.a) * 180 / Math.PI + 360) % 360,
      flip_v: matrix.a * matrix.d - matrix.b * matrix.c < 0,
    };
  };
  const opacityGroups = new Map();
  const painted = (element, object, paint = paintOrder.get(element)) => {
    const owner = element.nodeType === Node.TEXT_NODE ? element.parentElement : element;
    if (object.kind !== 'shape' && !elementTransform(owner).isIdentity) {
      transformError(owner, `${object.kind} capture requires unscaled, unrotated content`);
    }
    if (paint) objectPaintOrder.set(object, paint);
    for (let current = owner; current; current = current.parentElement) {
      const style = getComputedStyle(current), opacity = Number(style.opacity);
      if (style.display === 'contents' || opacity === 1) continue;
      const angle = (object.rotation || 0) * Math.PI / 180;
      const width = Math.abs(Math.cos(angle)) * object.w + Math.abs(Math.sin(angle)) * object.h;
      const height = Math.abs(Math.sin(angle)) * object.w + Math.abs(Math.cos(angle)) * object.h;
      const left = object.x + (object.w - width) / 2;
      const top = object.y + (object.h - height) / 2;
      if (!opacityGroups.has(current)) opacityGroups.set(current, {opacity, members: []});
      opacityGroups.get(current).members.push({
        object, bounds: {left, top, right: left + width, bottom: top + height},
        encoded: object.kind === 'svg' && current === owner,
      });
    }
    return object;
  };
  const compositeElements = [...root.querySelectorAll('table')].filter(
    (element) => visible(element) && [...element.rows].some(
      (row) => [...row.cells].some((cell) => visible(cell))
    )
  );
  const compositeIds = new Map(
    compositeElements.map((element) => [element, objectId(element, 'table')])
  );
  const tableGridOwner = (element) => {
    const table = element.closest('table');
    // Captions use ordinary content capture; the native grid owns rows and cells.
    return table && element.closest('caption')?.closest('table') !== table ? table : null;
  };
  const containerBinding = (element) => {
    const container = element.parentElement?.closest('table');
    const containerId = container ? compositeIds.get(container) : null;
    return containerId ? {container_id: containerId} : {};
  };
  for (const {element, pseudo} of paintVisibility.backgroundImages) {
    const owner = baseId(element) + (pseudo ? `::${pseudo}` : '');
    throw new Error(`CSS native paint on ${owner} requires native fills, borders or vector geometry in place of background images.`);
  }
  for (const table of compositeElements) {
    const boxes = [table, ...table.querySelectorAll('*')].filter((element) => visible(element));
    if (boxes.some((element) => !elementTransform(element).isIdentity)) {
      transformError(table, 'native tables require unscaled, unrotated cells and content');
    }
    if (boxes.some((element) => Number(getComputedStyle(element).opacity) !== 1)) {
      throw new Error(`CSS native paint on ${baseId(table)} requires an isolated opacity group for its table content. Use native fill alpha for translucent cell backgrounds.`);
    }
  }
  const appendBoxVisual = (element, role, paintBorders = true, pseudoPaint = null) => {
    const style = getComputedStyle(element, pseudoPaint ? `::${pseudoPaint.pseudo}` : null);
    const measured = pseudoPaint ? {
      x: (pseudoPaint.bounds[0] - scrollX - rootRect.left) / pxPerInch,
      y: (pseudoPaint.bounds[1] - scrollY - rootRect.top) / pxPerInch,
      w: pseudoPaint.bounds[2] / pxPerInch, h: pseudoPaint.bounds[3] / pxPerInch,
    } : (() => {
      const box = element.getBoundingClientRect();
      return {x: (box.left - rootRect.left) / pxPerInch, y: (box.top - rootRect.top) / pxPerInch,
        w: box.width / pxPerInch, h: box.height / pxPerInch};
    })();
    let transform = null;
    let bounds = pseudoPaint ? measured : rect(element);
    const append = (object) => {
      if (transform) {
        const {matrix, sx, sy, width, height, cx, cy, rotation, flip_v} = transform;
        const center = matrix.transformPoint({
          x: object.x + object.w / 2 - width / 2,
          y: object.y + object.h / 2 - height / 2,
        });
        Object.assign(object, {
          x: cx + center.x - object.w * sx / 2,
          y: cy + center.y - object.h * sy / 2,
          w: object.w * sx, h: object.h * sy, rotation, flip_v,
          radius: object.radius * sx, line_width_pt: object.line_width_pt * sx,
        });
        if (object.border_widths) {
          object.border_widths = Object.fromEntries(Object.entries(object.border_widths).map(
            ([side, width]) => [side, width * (['top', 'bottom'].includes(side) ? sy : sx)]
          ));
        }
      }
      if (pseudoPaint) {
        const table = element.closest('table');
        if (table && compositeIds.has(table)) object.container_id = compositeIds.get(table);
      }
      objects.push(painted(element, object, pseudoPaint || paintOrder.get(element)));
    };
    const alpha = colorPixel(style.backgroundColor)[3];
    if (pseudoPaint && (
        style.boxShadow !== 'none' || style.borderImageSource !== 'none' ||
        style.clipPath !== 'none' || style.maskImage !== 'none' ||
        Number(style.opacity) !== 1)) {
      throw new Error(`CSS native pseudo-element paint cannot represent the compositing on ${baseId(element)}::${pseudoPaint.pseudo}.`);
    }
    const hasFill = alpha > 0;
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
    transform = boxTransform(element, style, measured, pseudoPaint);
    if (transform) {
      if (style.boxShadow !== 'none' || style.borderImageSource !== 'none' ||
          style.clipPath !== 'none' || style.maskImage !== 'none') {
        transformError(element, 'box compositing requires additional native paint support');
      }
      if (activeBorders.some((side) => side.pattern !== 'solid')) {
        transformError(element, 'transformed borders require solid paint');
      }
      if (hasOutline && outlineStyle !== 'solid') {
        transformError(element, 'transformed outlines require solid paint');
      }
      bounds = {x: 0, y: 0, w: transform.width, h: transform.height};
    }
    const backdrop = backdropColor(element, Boolean(pseudoPaint));
    const fill = hasFill ? rgb(style.backgroundColor, null) : backdrop;
    const firstBorder = activeBorders[0];
    const uniformOutline = !transform && activeBorders.length === 4 && activeBorders.every(
      (side) => Math.abs(side.width - firstBorder.width) < 0.05 &&
        rgb(side.color) === rgb(firstBorder.color)
    );
    const outlineOnBounds = hasOutline && !uniformOutline &&
      Math.abs(outlineOffset + outlineWidth / 2) < 0.05;
    let radius = Number.parseFloat(style.borderRadius || '0') / pxPerInch;
    let uniformPixelRadius = false;
    try {
      const value = CSSNumericValue.parse(style.borderRadius);
      uniformPixelRadius = value.unit === 'px';
      if (value.unit === 'percent' && Math.abs(bounds.w - bounds.h) < 0.0001) {
        radius = value.value / 100 * bounds.w;
        uniformPixelRadius = true;
      }
    } catch (_) {}
    if (pseudoPaint && radius > 0 && !uniformPixelRadius) {
      throw new Error(`CSS native pseudo-element paint requires circular, uniform corner radii on ${baseId(element)}::${pseudoPaint.pseudo}.`);
    }
    if (transform && (!uniformPixelRadius ||
        ((radius > 0 || hasOutline) && Math.abs(transform.sx - transform.sy) > 1e-6))) {
      transformError(element, 'rounded corners and outlines require uniform scale and circular radii');
    }
    if (hasFill || uniformOutline || outlineOnBounds) {
      const frameColor = uniformOutline ? firstBorder.color :
        outlineOnBounds ? style.outlineColor : backdrop;
      const frameWidth = uniformOutline ? firstBorder.width :
        outlineOnBounds ? outlineWidth : 0;
      append({
        id: objectId(element, role), kind: 'shape', role,
        module_id: moduleId(element), ...bounds,
        fill,
        fill_enabled: hasFill,
        fill_alpha: alpha / 255,
        line: rgb(frameColor, backdrop),
        line_width_pt: frameWidth * 72 / pxPerInch,
        radius,
      });
    }
    // The solid border ring preserves curved single/unequal edges without covering
    // its transparent center. Other paint styles retain their existing path.
    const roundedSolidBorder = (radius > 0 || transform) && uniformPixelRadius &&
      activeBorders.length > 0 && sides.filter((side) => side.width > 0).every(
        (side) => side.pattern === 'solid' && !transparent(side.color) &&
          rgb(side.color, backdrop) === rgb(firstBorder.color, backdrop)
      );
    if (!uniformOutline && roundedSolidBorder) {
      const color = rgb(firstBorder.color, backdrop);
      append({
        id: objectId(element, `${role}-border`), kind: 'shape', role: `${role}-border`,
        module_id: moduleId(element), ...bounds, fill: color, fill_enabled: true,
        line: color, line_width_pt: 0,
        radius: Math.min(radius, bounds.w / 2, bounds.h / 2),
        border_widths: Object.fromEntries(sides.map((side) => [side.name, side.width / pxPerInch])),
      });
    } else if (!uniformOutline) {
      for (const border of activeBorders) {
        const thickness = border.width / pxPerInch;
        const geometry = radius === 0 && border.pattern === 'solid' ? {
          ...bounds,
          border_widths: Object.fromEntries(sides.map((side) => [side.name, side.width / pxPerInch])),
          border_side: border.name,
        } : border.name === 'top' ?
          {x: bounds.x, y: bounds.y, w: bounds.w, h: thickness} :
          border.name === 'right' ?
            {x: bounds.x + bounds.w - thickness, y: bounds.y, w: thickness, h: bounds.h} :
            border.name === 'bottom' ?
              {x: bounds.x, y: bounds.y + bounds.h - thickness, w: bounds.w, h: thickness} :
              {x: bounds.x, y: bounds.y, w: thickness, h: bounds.h};
        const color = rgb(border.color, backdrop);
        append({
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
      const left = transform ? bounds.x - outset : Math.max(0, bounds.x - outset);
      const top = transform ? bounds.y - outset : Math.max(0, bounds.y - outset);
      const right = transform ? bounds.x + bounds.w + outset : Math.min(pageWidth, bounds.x + bounds.w + outset);
      const bottom = transform ? bounds.y + bounds.h + outset : Math.min(pageHeight, bounds.y + bounds.h + outset);
      const color = rgb(style.outlineColor, backdrop);
      append({
        id: objectId(element, `${role}-outline`),
        kind: 'shape', role: `${role}-outline`, module_id: moduleId(element),
        x: left, y: top, w: right - left, h: bottom - top,
        fill: pageBackground, fill_enabled: false,
        line: color, line_width_pt: outlineWidth * 72 / pxPerInch, radius,
      });
    }
  };
  // Capture box paint once in DOM order: a shared surface precedes its children,
  // regardless of its HTML tag. Table layers are captured together below;
  // Office Math carries the expression, not the math element's CSS background/border.
  for (const element of [root, ...root.querySelectorAll('*')]) {
    const svg = element.closest('svg');
    const table = tableGridOwner(element);
    const box = element.getBoundingClientRect();
    if ((svg && (svg !== element || element.ownerSVGElement)) || (table && table !== element) || element.parentElement?.closest('math') ||
        !visible(element, false) || box.width <= 0 || box.height <= 0) continue;
    const role = element.hasAttribute('data-poster-module') ? 'module-frame' :
      element.hasAttribute('data-poster-title-band') ? 'title-band' : 'decoration';
    const firstObject = objects.length;
    appendBoxVisual(element, role, !(compositeIds.has(element) && collapsedTableBox(element)));
    const containerId = compositeIds.get(element) || containerBinding(element).container_id;
    if (containerId) {
      for (const item of objects.slice(firstObject)) item.container_id = containerId;
    }
  }
  for (const paint of pseudoPaints) {
    const element = paint.owner;
    if (!root.contains(element) || element.closest('svg') || !paintVisibility.visible(element, paint.pseudo)) continue;
    const style = getComputedStyle(element, `::${paint.pseudo}`);
    if (style.visibility === 'hidden' || Number(style.opacity) <= 0.02) continue;
    if (paint.pseudo !== 'marker' && style.content !== '""') {
      throw new Error(`Generated content has no native scene mapping: ::${paint.pseudo}. Use ordinary elements for editable content.`);
    }
    appendBoxVisual(element, `pseudo-${paint.pseudo}`, true, paint);
  }

  const tables = compositeElements;
  const measuredCells = new Set([...root.querySelectorAll('th,td')].filter(
    (cell) => cell.querySelector('math, table, img, svg') || hasListMarkers(cell) || hasPseudoPaint(cell)
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
        const prefix = selectionText(range).trimStart();
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
    // CSS table backgrounds paint by layer, before cell content. The native
    // table supplies editable text/grid geometry with transparent cell fills.
    // Use the table's paint key so its backgrounds precede its atomic text.
    const firstBackground = objects.length;
    for (const selector of ['colgroup', 'col']) {
      for (const part of element.querySelectorAll(selector)) {
        if (part.closest('table') !== element || !visible(part)) continue;
        appendBoxVisual(part, 'table-background', false);
      }
    }
    for (const {cell} of cells) {
      // Row/group paint extends across each originating cell's full span;
      // border-spacing gaps expose the table background.
      const layers = [];
      for (let parent = cell.parentElement; parent !== element; parent = parent.parentElement) {
        layers.unshift(parent);
      }
      for (const layer of layers) {
        const color = getComputedStyle(layer).backgroundColor;
        const alpha = colorPixel(color)[3];
        if (alpha) {
          objects.push(painted(layer, {
            id: objectId(cell, 'table-background'), kind: 'shape', role: 'table-background',
            module_id: moduleId(element), ...rect(cell),
            fill: rgb(color, null), fill_alpha: alpha / 255,
            line_width_pt: 0, radius: 0,
          }));
        }
      }
      appendBoxVisual(cell, 'table-background', false);
    }
    for (const item of objects.slice(firstBackground)) {
      item.container_id = compositeIds.get(element);
      objectPaintOrder.set(item, paintOrder.get(element));
    }
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
        font_face: face, color: rgb(style.color, fill), fill, fill_enabled: false,
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
        fill_enabled: false,
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
    objects.push(painted(element, {
      id: compositeIds.get(element), kind: 'table', role: 'evidence',
      module_id: moduleId(element), ...containerBinding(element), ...bounds, rows,
      row_heights: rowHeights, column_widths: columnWidths,
      cell_styles: cellStyles, cell_runs: cellRuns,
      merges: cells.filter((entry) => entry.row_span > 1 || entry.column_span > 1)
        .map(({row, column, row_span, column_span}) => ({row, column, row_span, column_span})),
    }));
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
          objects.push(painted(element, {
            id: objectId(element, 'table-border'), kind: 'shape', role: 'table-border',
            module_id: moduleId(element), container_id: compositeIds.get(element),
            ...geometry, fill: border.color, fill_enabled: true,
            line: border.color, line_width_pt: 0, radius: 0,
          }));
        }
      }
    }
  }

  // Cell fills precede their images; mixed-content text uses measured native runs.
  for (const element of root.querySelectorAll('img, svg')) {
    if (!visible(element)) continue;
    if (element.ownerSVGElement) continue;
    const imageSource = element.localName === 'img' ? element.currentSrc || element.src : '';
    const sourceFigureSha256 = element.localName === 'img'
      ? (element.getAttribute('data-source-figure-sha256') || '').trim() : '';
    if (sourceFigureSha256 && imageSource !== element.src) {
      throw new Error(`Source figure ${baseId(element)} renders a resource different from its validated img src. Keep its bound source image as the rendered resource.`);
    }
    const nativePptx = element.getAttribute('data-native-pptx');
    if (nativePptx) {
      const box = paintedImageSize(element, element.getBoundingClientRect());
      if (box.clipped) throw new Error(`Native component ${baseId(element)} clips its drawing.`);
      objects.push(painted(element, {
        id: objectId(element, 'component'), kind: 'component', role: 'diagram',
        module_id: moduleId(element), ...containerBinding(element),
        x: (box.left - rootRect.left) / pxPerInch, y: (box.top - rootRect.top) / pxPerInch,
        w: box.width / pxPerInch, h: box.height / pxPerInch,
        src: nativePptx, alt: element.getAttribute('alt') || '',
      }));
      continue;
    }
    if (nativeSvgs.has(element)) {
      const box = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      const inset = side => (Number.parseFloat(style[`border${side}Width`]) || 0) +
        (Number.parseFloat(style[`padding${side}`]) || 0);
      const width = box.width - inset('Left') - inset('Right');
      const height = box.height - inset('Top') - inset('Bottom');
      const copy = element.cloneNode(true);
      const originals = [element, ...element.querySelectorAll('*')];
      const clones = [copy, ...copy.querySelectorAll('*')];
      const properties = ['fill', 'fill-opacity', 'fill-rule', 'stroke', 'stroke-width',
        'stroke-opacity', 'stroke-linecap', 'stroke-linejoin', 'stroke-dasharray',
        'stroke-dashoffset', 'stroke-miterlimit', 'opacity', 'display', 'visibility',
        'clip-path', 'mask-image', 'filter', 'mix-blend-mode', 'isolation', 'vector-effect',
        'marker-start', 'marker-mid', 'marker-end', 'paint-order', 'overflow'];
      originals.forEach((node, index) => {
        const computed = getComputedStyle(node);
        svgPaintIndices.set(node, index);
        clones[index].setAttribute('data-poster-paint-index', String(index));
        clones[index].setAttribute('style', properties.map(
          name => `${name}:${computed.getPropertyValue(name)}`
        ).join(';'));
        for (const name of ['x', 'y', 'width', 'height', 'rx', 'ry', 'r', 'cx', 'cy']) {
          const value = computed.getPropertyValue(name);
          if (value && value !== 'auto' && value !== 'none') clones[index].setAttribute(name, value);
        }
        const path = computed.getPropertyValue('d');
        if (path && path !== 'none') clones[index].setAttribute('data-poster-css-path', path);
        if (index && typeof node.getCTM === 'function' && typeof node.parentElement.getCTM === 'function') {
          const local = node.parentElement.getCTM().inverse().multiply(node.getCTM());
          const values = [local.a, local.b, local.c, local.d, local.e, local.f];
          if (local.is2D === false || values.some(value => !Number.isFinite(value))) {
            throw new Error(`Native SVG ${baseId(element)} has unsupported geometry transforms.`);
          }
          clones[index].setAttribute('transform', `matrix(${values.join(',')})`);
        }
      });
      for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
        const ancestorStyle = getComputedStyle(ancestor);
        if (Number(ancestorStyle.opacity) !== 1 || ancestorStyle.filter !== 'none' ||
            ancestorStyle.clipPath !== 'none' || ancestorStyle.maskImage !== 'none' ||
            ancestorStyle.mixBlendMode !== 'normal') {
          throw new Error(`Native SVG ${baseId(element)} requires resolved ancestor compositing.`);
        }
      }
      for (const node of copy.querySelectorAll('text, style')) node.remove();
      copy.setAttribute('width', String(width));
      copy.setAttribute('height', String(height));
      copy.setAttribute('style', `${copy.getAttribute('style')};width:${width}px;height:${height}px`);
      const id = objectId(element, 'diagram');
      nativeSvgIds.set(element, id);
      objects.push(painted(element, {
        id, kind: 'svg', role: 'diagram', module_id: moduleId(element), ...containerBinding(element),
        x: (box.left + inset('Left') - rootRect.left) / pxPerInch,
        y: (box.top + inset('Top') - rootRect.top) / pxPerInch,
        w: width / pxPerInch, h: height / pxPerInch,
        svg: new XMLSerializer().serializeToString(copy),
      }));
      continue;
    }
    let src = '';
    let alt = '';
    if (element.tagName.toLowerCase() === 'svg') {
      const xml = new XMLSerializer().serializeToString(element);
      src = `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(xml)))}`;
      alt = element.getAttribute('aria-label') || 'Inline scientific figure';
    } else {
      src = imageSource;
      alt = element.getAttribute('alt') || '';
    }
    if (!src) continue;
    const box = paintedImageSize(element, element.getBoundingClientRect());
    if (box.clipped) {
      throw new Error(`Image ${baseId(element)} clips its source pixels inside the CSS content box.`);
    }
    objects.push(painted(element, {
      id: objectId(element, 'figure'), kind: 'image', role: 'figure',
      module_id: moduleId(element), ...containerBinding(element),
      x: (box.left - rootRect.left) / pxPerInch, y: (box.top - rootRect.top) / pxPerInch,
      w: box.width / pxPerInch, h: box.height / pxPerInch, src, alt,
      source_figure_sha256: sourceFigureSha256,
    }));
  }

  for (const svg of nativeSvgs) {
    if (!nativeSvgIds.has(svg)) continue;
    const walker = document.createTreeWalker(svg, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const owner = node.parentElement;
      if (!owner.closest('text') || !node.textContent.trim() || !visible(owner, false)) continue;
      const matrix = owner.getScreenCTM();
      if (Math.abs(matrix.b) > 0.00001 || Math.abs(matrix.c) > 0.00001) {
        throw new Error('Native SVG labels require unrotated text.');
      }
      const range = document.createRange();
      range.selectNodeContents(node);
      const text = selectionText(range);
      const box = rect(range);
      if (!text.trim() || box.w <= 0 || box.h <= 0) continue;
      const style = getComputedStyle(owner);
      for (let ancestor = owner; ancestor && ancestor !== svg.parentElement; ancestor = ancestor.parentElement) {
        const computed = getComputedStyle(ancestor);
        if (ancestor.localName === 'textPath' || ancestor.hasAttribute('textLength') ||
            ['x', 'y', 'dx', 'dy', 'rotate'].some(name => ancestor[name]?.baseVal?.numberOfItems > 1) ||
            (ancestor.rotate?.baseVal?.numberOfItems && ancestor.rotate.baseVal.getItem(0).value !== 0)) {
          throw new Error(`Native SVG ${baseId(svg)} requires labels with a single position and natural glyph geometry.`);
        }
        if (Number(computed.opacity) !== 1 || computed.filter !== 'none' || computed.clipPath !== 'none' ||
            computed.maskImage !== 'none' || computed.mixBlendMode !== 'normal') {
          throw new Error(`Native SVG ${baseId(svg)} requires resolved text compositing.`);
        }
      }
      if (Number(style.fillOpacity) !== 1 || colorPixel(style.fill)[3] !== 255 || style.stroke !== 'none' ||
          Math.abs(Math.abs(matrix.a) - Math.abs(matrix.d)) > 0.00001) {
        throw new Error(`Native SVG ${baseId(svg)} requires opaque, uniformly scaled text.`);
      }
      const confirmed = Boolean(platformFont(node));
      objects.push(painted(node, {
        id: objectId(owner, 'diagram-label'), kind: 'text', role: 'diagram-label',
        module_id: moduleId(owner), container_id: nativeSvgIds.get(svg), ...box, text,
        _svg_order: svgPaintIndices.get(owner),
        font_size_pt: Number.parseFloat(style.fontSize) * Math.abs(matrix.a) * 0.75,
        letter_spacing_pt: Number.parseFloat(style.letterSpacing) * Math.abs(matrix.a) * 0.75 || 0,
        font_face: fontFace(owner, node), color: rgb(style.fill, '111111'),
        bold: Number.parseInt(style.fontWeight, 10) >= 600, italic: style.fontStyle === 'italic',
        align: 'left', fit: confirmed ? 'none' : 'shrink', word_wrap: false,
        font_preflighted: confirmed,
      }));
    }
  }

  // Keep each MathML expression intact for native PowerPoint Office Math export.
  const mathElements = [...root.querySelectorAll('math')];
  const floatedBoxes = [...root.querySelectorAll('*')]
    .filter(element => getComputedStyle(element).float !== 'none' && visible(element))
    .map(element => {
      const box = element.getBoundingClientRect(), style = getComputedStyle(element);
      return {element,
        left: box.left - (Number.parseFloat(style.marginLeft) || 0),
        top: box.top - (Number.parseFloat(style.marginTop) || 0),
        right: box.right + (Number.parseFloat(style.marginRight) || 0),
        bottom: box.bottom + (Number.parseFloat(style.marginBottom) || 0)};
    });
  const mathObjects = [];
  const mathFlows = new Set();
  // mathml2omml emits m:nor for these literal tokens; Office retains their
  // explicit size instead of applying MathML's contextual script scaling.
  const normalTextSizes = math => [...math.querySelectorAll('mtext, ms')]
    .filter(node => !node.closest('annotation, annotation-xml') &&
      (!node.hasAttribute('mathvariant') || node.getAttribute('mathvariant') === 'normal'))
    .map(node => Number.parseFloat(getComputedStyle(node).fontSize) * 0.75);
  const textAlignment = style => style.textAlign === 'start' ? (style.direction === 'rtl' ? 'right' : 'left') :
    style.textAlign === 'end' ? (style.direction === 'rtl' ? 'left' : 'right') : style.textAlign;
  const inlineFlowOwner = (math) => {
    if (getComputedStyle(math).display !== 'math') return null;
    let owner = math.parentElement;
    while (owner && ['inline', 'contents'].includes(getComputedStyle(owner).display)) owner = owner.parentElement;
    if (!owner || !['block', 'flow-root', 'list-item', 'table-cell'].includes(getComputedStyle(owner).display)) return null;
    const elements = [...owner.querySelectorAll('*')].filter(element =>
      element.localName === 'math' || !element.closest('math'));
    // A native text flow owns inline content. Nested layout and media keep their
    // existing independently measured scene objects.
    if (elements.some(element => {
      const style = getComputedStyle(element);
      const inline = element.localName === 'math' ? style.display === 'math' :
        ['inline', 'contents'].includes(style.display);
      return !inline || element.matches('br,img,svg,canvas') ||
        style.position !== 'static' || style.float !== 'none' || style.verticalAlign !== 'baseline' ||
        style.whiteSpaceCollapse !== 'collapse' ||
        style.transform !== 'none' || style.translate !== 'none' ||
        !elementTransform(element).isIdentity;
    })) return null;
    const ownerStyle = getComputedStyle(owner);
    if (owner.getClientRects().length !== 1 || ownerStyle.writingMode !== 'horizontal-tb' ||
        ownerStyle.whiteSpaceCollapse !== 'collapse' ||
        Number.parseFloat(ownerStyle.textIndent) !== 0) return null;
    for (let ancestor = owner; ancestor; ancestor = ancestor.parentElement) {
      const style = getComputedStyle(ancestor);
      if (style.columnCount !== 'auto' || style.columnWidth !== 'auto') return null;
      if (ancestor === root) break;
    }
    return owner;
  };
  for (const math of mathElements) {
    if (!visible(math)) continue;
    const owner = inlineFlowOwner(math);
    if (owner) mathFlows.add(owner);
  }
  for (const owner of mathFlows) {
    const runs = [];
    const boxes = [];
    const walker = document.createTreeWalker(owner, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const element = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
      if (!visible(element, false)) continue;
      const style = getComputedStyle(element);
      if (element.localName === 'math' && node.nodeType === Node.ELEMENT_NODE) {
        if (!visible(element)) continue;
        boxes.push(element.getBoundingClientRect());
        runs.push({mathml: new XMLSerializer().serializeToString(element),
          normal_text_sizes_pt: normalTextSizes(element),
          font_size_pt: Number.parseFloat(style.fontSize) * 0.75,
          font_face: fontFace(element), color: rgb(style.color, '111111'), align: 'left'});
      } else if (node.nodeType === Node.TEXT_NODE && !element.closest('math')) {
        const range = document.createRange();
        range.selectNodeContents(node);
        const text = selectionText(range);
        if (text.trim()) boxes.push(...range.getClientRects());
        if (text) runs.push({text, font_size_pt: Number.parseFloat(style.fontSize) * 0.75,
          font_face: fontFace(element, node), color: rgb(style.color, '111111'),
          bold: Number.parseInt(style.fontWeight, 10) >= 600, italic: style.fontStyle === 'italic',
          ...textSpacing(style)});
      }
    }
    const paintedBoxes = boxes.filter(box => box.width > 0 && box.height > 0);
    if (!runs.length || !paintedBoxes.length) {
      mathFlows.delete(owner); continue;
    }
    const style = getComputedStyle(owner), box = owner.getBoundingClientRect();
    // A float's margin box shapes nearby lines; a floated ancestor owns its flow.
    if (floatedBoxes.some(float => !float.element.contains(owner) &&
      float.left < box.right && float.right > box.left && float.top < box.bottom && float.bottom > box.top)) {
      mathFlows.delete(owner); continue;
    }
    const align = textAlignment(style);
    const cellInsets = globalThis.__posterCellInsets?.get(owner);
    const inset = side => cellInsets ? cellInsets[side === 'left' ? 3 : 1] :
      Number.parseFloat(style.getPropertyValue(`padding-${side}`)) +
      Number.parseFloat(style.getPropertyValue(`border-${side}-width`));
    const left = Math.min(...paintedBoxes.map(box => box.left));
    const right = Math.max(...paintedBoxes.map(box => box.right));
    const top = Math.min(...paintedBoxes.map(box => box.top));
    const bottom = Math.max(...paintedBoxes.map(box => box.bottom));
    // Extra leading space from floats or bidi displacement is already painted.
    const start = ['center', 'right'].includes(align) ? box.left + inset('left') : left;
    const end = align === 'right' ? right : box.right - inset('right');
    mathObjects.push(painted(owner, {
      id: objectId(owner, 'native-equation'), kind: 'equation', role: 'equation',
      module_id: moduleId(owner), ...containerBinding(owner),
      x: (start - rootRect.left) / pxPerInch,
      y: (top - rootRect.top) / pxPerInch,
      w: (end - start) / pxPerInch, h: (bottom - top) / pxPerInch,
      runs, font_size_pt: Number.parseFloat(style.fontSize) * 0.75,
      font_face: fontFace(owner), color: rgb(style.color, '111111'),
      align: ['center', 'right'].includes(align) ? align : 'left',
      rtl: style.direction === 'rtl',
    }));
  }
  const inMathFlow = element => [...mathFlows].some(owner => owner === element || owner.contains(element));
  for (const math of mathElements) {
    if (!visible(math) || inMathFlow(math)) continue;
    const style = getComputedStyle(math);
    // Native math retains the CSS allocation, anchor and mathematical face.
    const content = document.createRange();
    content.selectNodeContents(math);
    const box = math.getBoundingClientRect();
    const inset = side => Number.parseFloat(style.getPropertyValue(`padding-${side}`)) +
      Number.parseFloat(style.getPropertyValue(`border-${side}-width`));
    const left = box.left + inset('left'), right = box.right - inset('right');
    const align = math.getAttribute('display') === 'block' ? 'center' : textAlignment(style);
    mathObjects.push(painted(math, {
      id: objectId(math, 'native-equation'), kind: 'equation', role: 'equation',
      module_id: moduleId(math), ...containerBinding(math), ...rect(content),
      x: (left - rootRect.left) / pxPerInch, w: (right - left) / pxPerInch,
      mathml: new XMLSerializer().serializeToString(math),
      normal_text_sizes_pt: normalTextSizes(math),
      latex: math.getAttribute('data-latex') || '',
      font_size_pt: Number.parseFloat(style.fontSize || '16') * 0.75,
      font_face: fontFace(math), color: rgb(style.color, '111111'),
      align: ['center', 'right'].includes(align) ? align : 'left',
    }));
  }

  const explicitText = 'h1, h2, h3, h4, p, li, caption, figcaption, [data-poster-text], [role="rowheader"]';
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
    if (tableGridOwner(element)) return measuredCells.has(element);
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
  const textFragments = (element) => {
    // Chromium's painted runs retain shaping, transforms and soft-wrap boundaries.
    // DOM Range unions can include the preceding line's generated hyphen.
    const fragments = [];
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const selection = getSelection();
    try {
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const owner = node.parentElement;
        if (!visible(owner, false) || owner.closest('math, svg, style, script')) continue;
        if (tableGridOwner(owner) && !measuredCells.has(owner.closest('th, td'))) continue;
        const style = getComputedStyle(owner);
        const fontSize = Number.parseFloat(style.fontSize || '16');
        const lineHeight = Number.parseFloat(style.lineHeight) || fontSize * 1.2;
        const runs = [...globalThis.__posterTextRuns.get(node) || []].sort((a, b) => a.start - b.start);
        const range = document.createRange();
        range.selectNodeContents(node);
        const paintedText = selectionText(range);
        let paintedEnd = 0;
        const usedRects = new Map();
        for (const run of runs) {
          const {start, length, bounds: [, , shapedWidth, shapedHeight]} = run;
          // CDP starts use DOM offsets while lengths count shaped characters.
          // Browser prefixes map collapsed whitespace and contextual transforms;
          // successive boxes may split the expansion of one source character.
          range.setEnd(node, start);
          const paintedStart = Math.max(paintedEnd, selectionText(range).length);
          const text = paintedText.slice(paintedStart, paintedStart + length);
          paintedEnd = paintedStart + length;
          if (!text.trim() || shapedWidth <= 0 || shapedHeight <= 0) continue;
          // Snapshot offsets preserve shaping; bounded DOM ranges resolve physical
          // fragment positions, including columns and transformed character runs.
          const end = runs.find((next) => next.start > start)?.start ?? node.length;
          const fragmentRange = document.createRange();
          fragmentRange.setStart(node, start);
          fragmentRange.setEnd(node, end);
          const used = usedRects.get(start) || new Set();
          const rects = [...fragmentRange.getClientRects()].map((box, index) => ({box, index}))
            .filter(({box, index}) => box.width > 0 && box.height > 0 && !used.has(index));
          const distance = ({box}) => Math.abs(box.width - shapedWidth) + Math.abs(box.height - shapedHeight);
          const matched = rects.reduce((best, candidate) => distance(candidate) < distance(best) ? candidate : best);
          used.add(matched.index);
          usedRects.set(start, used);
          const {left, top, width, height} = matched.box;
          fragments.push({
            owner, node, text,
            bounds: {
              left, top: top - Math.max(0, lineHeight - height) / 2,
              width, height: Math.max(lineHeight, height),
            },
          });
        }
      }
    } finally {
      selection.removeAllRanges();
    }
    return fragments;
  };
  for (const element of textElements) {
    if (!visible(element, false)) continue;
    // Chromium owns line breaks, alignment and fragmented flows such as columns.
    // Each painted run remains editable at its captured location in PowerPoint.
    for (const fragment of textFragments(element)) {
      // A container's direct text may share a subtree with headings and modules.
      // Preserve each painted run's semantic owner, not the traversal root's role.
      const owner = fragment.owner;
      if (inMathFlow(owner)) continue;
      const heading = owner.closest('h1, h2, h3, h4');
      const role = heading ? (heading.tagName.toLowerCase() === 'h1' ? 'title' : 'heading') :
        owner.closest('caption,figcaption,[data-content-role="caption"],.fig-caption') ? 'caption' : 'body';
      const style = getComputedStyle(fragment.owner);
      const fontSource = fragment.node || fragment.owner;
      const fontFaceValue = fontFace(fragment.owner, fontSource);
      const fontSizePx = Number.parseFloat(style.fontSize || '16');
      const fontPreflighted = Boolean(platformFont(fontSource));
      const bounds = {
        x: Math.max(0, (fragment.bounds.left - rootRect.left) / pxPerInch),
        y: Math.max(0, (fragment.bounds.top - rootRect.top) / pxPerInch),
        w: fragment.bounds.width / pxPerInch, h: fragment.bounds.height / pxPerInch,
      };
      objects.push(painted(fragment.node || owner, {
        id: objectId(owner, role), kind: 'text', role,
        module_id: moduleId(owner), ...containerBinding(owner), ...bounds,
        text: fragment.text,
        font_size_pt: fontSizePx * 0.75,
        font_face: fontFaceValue,
        ...textSpacing(style),
        // Chromium has already positioned this independent painted line.
        // Its native paragraph uses the captured allocation, including inline scripts.
        line_height_pt: fragment.bounds.height * 0.75,
        color: rgb(style.color, '111111'),
        bold: Number.parseInt(style.fontWeight || '400', 10) >= 600,
        italic: style.fontStyle === 'italic',
        align: 'left',
        // Preserve authored typography when Chromium confirmed the rendered face.
        fit: fontPreflighted ? 'none' : 'shrink', word_wrap: false,
        font_preflighted: fontPreflighted,
      }));
    }
  }
  for (const [element, {runs, faces}] of listMarkers) {
    if (!root.contains(element) || !visible(element)) continue;
    const style = getComputedStyle(element, '::marker');
    const ownerBounds = element.getBoundingClientRect();
    const backdrop = backdropColor(element);
    const face = faces.length === 1 ? faces[0] :
      style.fontFamily.split(',')[0].replaceAll('"', '').replaceAll("'", '').trim();
    for (const {text, bounds: [x, y, w, h]} of runs) {
      if (!text.trim() || w <= 0 || h <= 0) continue;
      // Outside markers do not paint over the list item's own background.
      const inside = x + w / 2 >= ownerBounds.left && x + w / 2 <= ownerBounds.right &&
        y + h / 2 >= ownerBounds.top && y + h / 2 <= ownerBounds.bottom;
      const background = inside ?
        rgb(getComputedStyle(element).backgroundColor, backdrop) : backdrop;
      objects.push(painted(element, {
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
      }));
    }
  }
  // Text backgrounds must not paint over native math.
  objects.push(...mathObjects);
  for (const [owner, {opacity, members}] of opacityGroups) {
    // A serialized SVG already carries its own opacity into native vector capture.
    if (members.length === 1 && members[0].encoded) continue;
    const style = getComputedStyle(owner);
    const unsupported = style.filter !== 'none' || style.backdropFilter !== 'none' ||
      style.clipPath !== 'none' || style.maskImage !== 'none' || style.mixBlendMode !== 'normal' ||
      members.some(({object, encoded}) => encoded || object.kind !== 'shape' ||
        object.fill_enabled === false || object.line_width_pt > 0);
    // Disjoint paints can retain group opacity as per-shape alpha. Intersecting
    // paints require an isolated native group to preserve their shared composite.
    const overlaps = members.some(({bounds: a}, index) => members.slice(index + 1).some(({bounds: b}) =>
      Math.min(a.right, b.right) > Math.max(a.left, b.left) &&
      Math.min(a.bottom, b.bottom) > Math.max(a.top, b.top)));
    if (unsupported || overlaps) {
      throw new Error(`CSS native paint on ${baseId(owner)} requires an isolated opacity group for this composition. Use per-paint alpha where the native objects support it.`);
    }
    for (const {object} of members) object.fill_alpha = (object.fill_alpha ?? 1) * opacity;
  }
  objects.sort((left, right) => {
    const a = objectPaintOrder.get(left)?.key, b = objectPaintOrder.get(right)?.key;
    if (!a || !b) return 0;
    for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
      if (a[index] !== b[index]) return a[index] - b[index];
    }
    return a.length - b.length;
  });
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
