() => {
  const root = document.querySelector(__POSTER_ROOT_SELECTOR__);
  if (!root) throw new Error('poster root was not found');
  const rootRect = root.getBoundingClientRect();
  const pageStyle = getComputedStyle(root);
  const pxPerInch = 96;
  const cleanId = (value) => String(value || 'poster')
    .trim().replace(/[^A-Za-z0-9._:-]+/g, '-').replace(/^-+|-+$/g, '') || 'poster';
  const visible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || '1') > 0.02 && rect.width > 1 && rect.height > 1;
  };
  const rgb = (value, fallback = '111111') => {
    const match = String(value || '').match(/rgba?\((\d+)[, ]+(\d+)[, ]+(\d+)/i);
    if (!match) {
      const hex = String(value || '').replace('#', '').trim();
      return /^[0-9a-f]{6}$/i.test(hex) ? hex.toUpperCase() : fallback;
    }
    return match.slice(1, 4).map((part) => Number(part).toString(16).padStart(2, '0')).join('').toUpperCase();
  };
  const fontFace = (value) => {
    const faces = String(value || '').split(',')
      .map((face) => face.replace(/["']/g, '').trim()).filter(Boolean);
    const portable = new Set([
      'Arial', 'Aptos', 'Calibri', 'Cambria', 'Courier New', 'Georgia',
      'Tahoma', 'Times New Roman', 'Trebuchet MS', 'Verdana'
    ]);
    return faces.find((face) => portable.has(face)) || 'Arial';
  };
  const fontReady = (face, sizePx, text) => Boolean(
    document.fonts && document.fonts.check(`${sizePx}px "${face}"`, text)
  );
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
  const cssContentText = (value) => {
    const content = String(value || '').trim();
    const match = content.match(/^(["'])(.*)\1$/s);
    if (!match) return '';
    return match[2]
      .replace(/\\([0-9a-f]{1,6})\s?/gi, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
      .replace(/\\([\\"'])/g, '$1');
  };
  const simpleListMarker = (element) => {
    if (element.tagName.toLowerCase() !== 'li') return '';
    if (getComputedStyle(element).listStyleType !== 'none') return '•';
    const marker = cssContentText(getComputedStyle(element, '::before').content);
    const glyphs = [...marker];
    return glyphs.length > 0 && glyphs.length <= 3 && !/\s/.test(marker) ? marker : '';
  };
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
  const portableTextScale = (element, text, face, wordWrap) => {
    const style = getComputedStyle(element);
    const bounds = contentBox(element);
    const fontSize = Number.parseFloat(style.fontSize || '16');
    const parsedLineHeight = Number.parseFloat(style.lineHeight || '');
    const lineHeight = Number.isFinite(parsedLineHeight) ? parsedLineHeight : fontSize * 1.2;
    const probe = document.createElement('div');
    Object.assign(probe.style, {
      position: 'fixed', left: '-100000px', top: '0',
      boxSizing: 'border-box', width: `${bounds.width}px`, height: 'auto',
      margin: '0', padding: '0', border: '0', overflow: 'visible',
      fontFamily: face, fontWeight: style.fontWeight, fontStyle: style.fontStyle,
      fontVariant: style.fontVariant, letterSpacing: 'normal',
      textTransform: style.textTransform, textAlign: style.textAlign,
      whiteSpace: wordWrap ? 'normal' : 'nowrap',
      wordBreak: style.wordBreak, overflowWrap: style.overflowWrap,
    });
    probe.textContent = text;
    document.body.appendChild(probe);
    const fits = (scale) => {
      probe.style.fontSize = `${fontSize * scale}px`;
      probe.style.lineHeight = `${lineHeight * scale}px`;
      return probe.scrollWidth <= bounds.width + 0.5 &&
        probe.scrollHeight <= bounds.height + 0.5;
    };
    if (fits(1)) {
      probe.remove();
      return 1;
    }
    let low = 0.25;
    let high = 1;
    for (let index = 0; index < 12; index += 1) {
      const middle = (low + high) / 2;
      if (fits(middle)) low = middle;
      else high = middle;
    }
    probe.remove();
    return Math.max(0.25, low * 0.98);
  };
  const transparent = (value) => /rgba\([^)]*,\s*0(?:\.0+)?\)/i.test(String(value)) || value === 'transparent';
  const pageBackground = transparent(pageStyle.backgroundColor) ?
    rgb(getComputedStyle(document.body).backgroundColor, 'FFFFFF') :
    rgb(pageStyle.backgroundColor, 'FFFFFF');
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
  const scaleLengths = (values, total) => {
    const sum = values.reduce((accumulator, value) => accumulator + value, 0);
    if (!(sum > 0)) return values;
    return values.map((value) => value * total / sum);
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
  const appendBoxVisual = (element, role) => {
    const style = getComputedStyle(element);
    const bounds = rect(element);
    const hasFill = !transparent(style.backgroundColor);
    const outlineWidth = Number.parseFloat(style.outlineWidth || '0');
    const outlineStyle = String(style.outlineStyle || 'none').toLowerCase();
    const hasOutline = outlineWidth > 0.5 &&
      !['none', 'hidden'].includes(outlineStyle) && !transparent(style.outlineColor);
    const sides = [
      ['top', style.borderTopWidth, style.borderTopColor],
      ['right', style.borderRightWidth, style.borderRightColor],
      ['bottom', style.borderBottomWidth, style.borderBottomColor],
      ['left', style.borderLeftWidth, style.borderLeftColor],
    ].map(([name, width, color]) => ({
      name,
      width: Number.parseFloat(width || '0'),
      color,
    }));
    const activeBorders = sides.filter(
      (side) => side.width > 0.5 && !transparent(side.color)
    );
    if (!hasFill && activeBorders.length === 0 && !hasOutline) return;
    const fill = transparent(style.backgroundColor) ? pageBackground :
      rgb(style.backgroundColor, 'FFFFFF');
    const firstBorder = activeBorders[0];
    const uniformOutline = activeBorders.length === 4 && activeBorders.every(
      (side) => Math.abs(side.width - firstBorder.width) < 0.05 &&
        rgb(side.color) === rgb(firstBorder.color)
    );
    const radius = Number.parseFloat(style.borderRadius || '0') / pxPerInch;
    if (hasFill || uniformOutline) {
      objects.push({
        id: objectId(element, role), kind: 'shape', role,
        module_id: moduleId(element), ...bounds,
        fill,
        fill_enabled: hasFill,
        line: uniformOutline ? rgb(firstBorder.color, pageBackground) : pageBackground,
        line_width_pt: uniformOutline ? firstBorder.width * 72 / pxPerInch : 0,
        radius,
      });
    }
    if (!uniformOutline) {
      for (const border of activeBorders) {
        const thickness = border.width / pxPerInch;
        const geometry = border.name === 'top' ?
          {x: bounds.x, y: bounds.y, w: bounds.w, h: thickness} :
          border.name === 'right' ?
            {x: bounds.x + bounds.w - thickness, y: bounds.y, w: thickness, h: bounds.h} :
            border.name === 'bottom' ?
              {x: bounds.x, y: bounds.y + bounds.h - thickness, w: bounds.w, h: thickness} :
              {x: bounds.x, y: bounds.y, w: thickness, h: bounds.h};
        const color = rgb(border.color, pageBackground);
        objects.push({
          id: objectId(element, `${role}-border-${border.name}`),
          kind: 'shape', role: `${role}-border`, module_id: moduleId(element),
          ...geometry, fill: color, fill_enabled: true,
          line: color, line_width_pt: 0, radius: 0,
        });
      }
    }
    if (hasOutline) {
      const offset = Number.parseFloat(style.outlineOffset || '0');
      const outset = (offset + outlineWidth / 2) / pxPerInch;
      const pageWidth = rootRect.width / pxPerInch;
      const pageHeight = rootRect.height / pxPerInch;
      const left = Math.max(0, bounds.x - outset);
      const top = Math.max(0, bounds.y - outset);
      const right = Math.min(pageWidth, bounds.x + bounds.w + outset);
      const bottom = Math.min(pageHeight, bounds.y + bounds.h + outset);
      const color = rgb(style.outlineColor, pageBackground);
      objects.push({
        id: objectId(element, `${role}-outline`),
        kind: 'shape', role: `${role}-outline`, module_id: moduleId(element),
        x: left, y: top, w: right - left, h: bottom - top,
        fill: pageBackground, fill_enabled: false,
        line: color, line_width_pt: outlineWidth * 72 / pxPerInch, radius,
      });
    }
  };
  const frameElements = [
    ...root.querySelectorAll('[data-poster-title-band], [data-poster-module], [data-poster-decor]')
  ];
  for (const element of frameElements) {
    if (!visible(element)) continue;
    const role = element.hasAttribute('data-poster-module') ? 'module-frame' :
      element.hasAttribute('data-poster-title-band') ? 'title-band' : 'decoration';
    appendBoxVisual(element, role);
  }

  const frameSet = new Set(frameElements);
  const visualElements = [...root.querySelectorAll('[data-poster-decor], div, span')];
  for (const element of visualElements) {
    if (!visible(element) || frameSet.has(element) ||
        element.matches('[data-poster-body], [data-poster-title-band], [data-poster-module]') ||
        element.closest('figure, table')) continue;
    appendBoxVisual(element, 'decoration');
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

  const tables = [...root.querySelectorAll('table')];
  for (const element of tables) {
    if (!visible(element)) continue;
    const rows = [...element.rows].map((row) => [...row.cells].map((cell) => cell.innerText.trim()));
    if (!rows.length || !rows[0].length) continue;
    const bounds = rect(element);
    const rowHeights = scaleLengths(
      [...element.rows].map((row) => row.getBoundingClientRect().height / pxPerInch),
      bounds.h
    );
    const columnWidths = scaleLengths(
      [...element.rows[0].cells].map((cell) => cell.getBoundingClientRect().width / pxPerInch),
      bounds.w
    );
    const style = getComputedStyle(element);
    const firstHeader = element.querySelector('th');
    const firstBody = element.querySelector('tbody td, tr:nth-child(n+2) td, td');
    const headerStyle = firstHeader ? getComputedStyle(firstHeader) : style;
    const bodyStyle = firstBody ? getComputedStyle(firstBody) : style;
    objects.push({
      id: objectId(element, 'table'), kind: 'table', role: 'evidence',
      module_id: moduleId(element), ...bounds, rows,
      row_heights: rowHeights, column_widths: columnWidths,
      font_size_pt: Number.parseFloat(bodyStyle.fontSize || style.fontSize || '16') * 0.75,
      font_face: fontFace(bodyStyle.fontFamily || style.fontFamily || 'Arial'),
      color: rgb(bodyStyle.color, '111111'),
      fill: transparent(bodyStyle.backgroundColor) ? 'FFFFFF' : rgb(bodyStyle.backgroundColor, 'FFFFFF'),
      line: rgb(bodyStyle.borderColor, 'D8D8D8'),
      header_fill: transparent(headerStyle.backgroundColor) ? 'FFFFFF' : rgb(headerStyle.backgroundColor, 'FFFFFF'),
      header_color: rgb(headerStyle.color, '111111'),
      body_fill: transparent(bodyStyle.backgroundColor) ? 'FFFFFF' : rgb(bodyStyle.backgroundColor, 'FFFFFF'),
      body_color: rgb(bodyStyle.color, '111111'),
    });
  }

  // Keep each MathML expression intact for native PowerPoint Office Math export.
  const mathElements = [...root.querySelectorAll('math')];
  for (const [mathIndex, math] of mathElements.entries()) {
    if (!visible(math)) continue;
    const style = getComputedStyle(math);
    objects.push({
      id: objectId(math, 'native-equation'), kind: 'equation', role: 'equation',
      module_id: moduleId(math), ...rect(math),
      mathml: new XMLSerializer().serializeToString(math),
      latex: math.getAttribute('data-latex') || '',
      fallback_src: '', math_index: mathIndex,
      font_size_pt: Number.parseFloat(style.fontSize || '16') * 0.75,
      font_face: 'Cambria Math', color: rgb(style.color, '111111'),
      align: ['left', 'right'].includes(style.textAlign) ? style.textAlign : 'center',
    });
  }

  const explicitText = 'h1, h2, h3, h4, p, li, figcaption, [data-poster-text]';
  const hasDirectText = (element) => [...element.childNodes].some(
    (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim()
  );
  const textCandidates = [
    ...new Set([
      ...root.querySelectorAll(explicitText),
      ...[...root.querySelectorAll('div, span')].filter(hasDirectText),
    ])
  ].filter((element) => {
    if (element.closest('table')) return false;
    if (element.closest('math') || element.matches('[data-content-role="equation"]')) return false;
    if (element.matches(explicitText)) return true;
    if (element.matches('[data-poster-body], [data-poster-title-band], [data-poster-module]')) return false;
    return true;
  });
  const textCandidateSet = new Set(textCandidates);
  const textElements = textCandidates.filter((element) => {
    for (let parent = element.parentElement; parent && parent !== root; parent = parent.parentElement) {
      if (textCandidateSet.has(parent)) return false;
    }
    return true;
  });
  for (const element of textElements) {
    if (!visible(element)) continue;
    let text = element.innerText.replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const tag = element.tagName.toLowerCase();
    const marker = simpleListMarker(element);
    if (marker) text = `${marker} ${text}`;
    const role = tag === 'h1' ? 'title' : ['h2', 'h3', 'h4'].includes(tag) ? 'heading' :
      element.closest('figcaption,[data-content-role="caption"],.fig-caption')
        ? 'caption' : 'body';
    const style = getComputedStyle(element);
    const fontFaceValue = fontFace(style.fontFamily || 'Arial');
    const wordWrap = textLineCount(element) > 1;
    const fontScale = portableTextScale(element, text, fontFaceValue, wordWrap);
    const fontSizePx = Number.parseFloat(style.fontSize || '16') * fontScale;
    if (!transparent(style.backgroundColor)) {
      const backgroundId = objectId(element, `${role}-background`);
      objects.push({
        id: backgroundId, kind: 'shape', role: `${role}-background`,
        module_id: moduleId(element), ...rect(element),
        fill: rgb(style.backgroundColor, 'FFFFFF'),
        line: rgb(style.backgroundColor, 'FFFFFF'),
        fill_enabled: true, line_width_pt: 0,
        radius: Number.parseFloat(style.borderRadius || '0') / pxPerInch,
      });
    }
    objects.push({
      id: objectId(element, role), kind: 'text', role,
      module_id: moduleId(element), ...textRect(element), text,
      font_size_pt: fontSizePx * 0.75,
      font_face: fontFaceValue,
      color: rgb(style.color, '111111'),
      bold: Number.parseInt(style.fontWeight || '400', 10) >= 600,
      italic: style.fontStyle === 'italic',
      align: ['center', 'right'].includes(style.textAlign) ? style.textAlign : 'left',
      fit: 'shrink', word_wrap: wordWrap,
      font_preflighted: fontReady(fontFaceValue, fontSizePx, text),
      font_scale: fontScale,
    });
  }
  return {
    schema: 'scientific-poster.pptx-scene.v2',
    page: {
      width_in: rootRect.width / pxPerInch,
      height_in: rootRect.height / pxPerInch,
      background: pageBackground
    },
    objects,
  };
}
