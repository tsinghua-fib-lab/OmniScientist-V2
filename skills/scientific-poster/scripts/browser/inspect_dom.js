() => {
  const warnings = [];
  const roots = Array.from(document.querySelectorAll(__POSTER_ROOT_SELECTOR__));
  const idCounts = new Map();
  for (const node of document.querySelectorAll('[data-poster-id]')) {
    const id = (node.getAttribute('data-poster-id') || '').trim();
    if (!id) {
      warnings.push({code: 'empty_poster_id', message: 'data-poster-id must be non-empty.'});
    } else {
      idCounts.set(id, (idCounts.get(id) || 0) + 1);
    }
  }
  for (const [posterId, count] of idCounts.entries()) {
    if (count > 1) warnings.push({
      code: 'duplicate_poster_id', poster_id: posterId, count,
      message: 'Every data-poster-id must be unique.'
    });
  }
  if (roots.length !== 1) {
    warnings.push({
      code: 'invalid_poster_root_count', count: roots.length,
      message: 'Exactly one body-level poster root is required.'
    });
    return {
      poster: null, modules: [], title_band: null, typography: [],
      source_figures: {
        count: 0, visible_count: 0, rendered_count: 0, usable_count: 0,
        referenced_sha256s: [], visible_sha256s: [], usable_sha256s: [], details: []
      },
      elements: [], text_runs: [], visual_targets: [], warnings
    };
  }

  const poster = roots[0];
  const posterRect = poster.getBoundingClientRect();
  const posterArea = posterRect.width * posterRect.height;
  const pxToMm = 25.4 / 96;
  if (!(posterRect.width > 0 && posterRect.height > 0)) {
    warnings.push({code: 'zero_poster_rect', message: 'Poster has no rendered area.'});
  }
  function visible(element) {
    if (!element || element.getClientRects().length === 0) return false;
    let opacity = 1;
    for (let current = element; current; current = current.parentElement) {
      const style = window.getComputedStyle(current);
      opacity *= Number(style.opacity || 1);
      if (
        style.display === 'none' || style.visibility === 'hidden' ||
        style.visibility === 'collapse' || opacity < 0.1
      ) return false;
      if (current === poster) break;
    }
    return true;
  }

  function inside(inner, outer, tolerance = 1) {
    return (
      inner.left >= outer.left - tolerance &&
      inner.top >= outer.top - tolerance &&
      inner.right <= outer.right + tolerance &&
      inner.bottom <= outer.bottom + tolerance
    );
  }

  function paintsBottom(style) {
    if (!style) return false;
    const transparent = (value) =>
      /rgba\([^)]*,\s*0(?:\.0+)?\)/i.test(String(value)) || value === 'transparent';
    if (!transparent(style.backgroundColor)) return true;
    return ['Right', 'Bottom', 'Left'].some((side) =>
      Number.parseFloat(style[`border${side}Width`] || '0') > 0.5 &&
      !['none', 'hidden'].includes(style[`border${side}Style`]) &&
      !transparent(style[`border${side}Color`]));
  }

  function clippingReason(element, rect) {
    let masked = false;
    for (let current = element.parentElement; current; current = current.parentElement) {
      const style = window.getComputedStyle(current);
      if (
        (style.clipPath && style.clipPath !== 'none') ||
        (style.maskImage && style.maskImage !== 'none') ||
        (style.webkitMaskImage && style.webkitMaskImage !== 'none')
      ) masked = true;
      const overflowClips = [style.overflow, style.overflowX, style.overflowY]
        .some((value) => value === 'hidden' || value === 'clip');
      if (overflowClips && !inside(rect, current.getBoundingClientRect())) return 'overflow';
      if (current === poster) break;
    }
    return masked ? 'mask' : null;
  }

  function clipped(element, rect) {
    return clippingReason(element, rect) !== null;
  }

  function hasContent(element) {
    const text = (element.innerText || element.textContent || '').trim();
    return Boolean(text) || Boolean(element.querySelector('img,svg,canvas,table'));
  }

  function relativeRect(rect) {
    return {
      left: rect.left - posterRect.left,
      top: rect.top - posterRect.top,
      width: rect.width,
      height: rect.height
    };
  }

  function layoutAncestorChain(element) {
    const chain = [];
    for (
      let current = element.parentElement;
      current && chain.length < 6;
      current = current.parentElement
    ) {
      const style = getComputedStyle(current);
      const item = {
        tag: current.tagName.toLowerCase(),
        id: current.id || '',
        classes: Array.from(current.classList),
        poster_id: (
          current.getAttribute('data-poster-id') ||
          current.getAttribute('data-poster-module') || ''
        ).trim(),
        rect: relativeRect(current.getBoundingClientRect()),
        display: style.display || '',
        paints_bottom: paintsBottom(style)
      };
      if (style.display === 'flex' || style.display === 'inline-flex') {
        item.flex_direction = style.flexDirection || '';
        if (style.flexWrap !== 'nowrap') item.flex_wrap = style.flexWrap;
        if (style.justifyContent !== 'normal') item.justify_content = style.justifyContent;
        if (style.alignItems !== 'normal') item.align_items = style.alignItems;
        if (style.alignContent !== 'normal') item.align_content = style.alignContent;
      }
      if (style.display === 'grid' || style.display === 'inline-grid') {
        item.grid_template_columns = style.gridTemplateColumns || '';
        item.grid_template_rows = style.gridTemplateRows || '';
      }
      if (style.gap !== 'normal' && style.gap !== '0px') item.gap = style.gap;
      if (
        style.flexGrow !== '0' || style.flexShrink !== '1' || style.flexBasis !== 'auto'
      ) {
        item.flex_grow = style.flexGrow;
        item.flex_shrink = style.flexShrink;
        item.flex_basis = style.flexBasis;
      }
      if (style.gridRowStart !== 'auto') item.grid_row_start = style.gridRowStart;
      if (style.gridColumnStart !== 'auto') item.grid_column_start = style.gridColumnStart;
      chain.push(item);
      if (current === poster) break;
    }
    return chain;
  }

  const elements = Array.from(
    document.querySelectorAll('[data-poster-id],[data-poster-module]')
  ).map((element) => {
    const posterId = (
      element.getAttribute('data-poster-id') ||
      element.getAttribute('data-poster-module') || ''
    ).trim();
    const rect = element.getBoundingClientRect();
    const isVisible = visible(element);
    const content = isVisible && hasContent(element);
    if (isVisible && !(rect.width > 0 && rect.height > 0)) {
      warnings.push({
        code: 'zero_element_rect', poster_id: posterId,
        message: 'A visible poster element has no rendered area.'
      });
    }
    if (isVisible && !inside(rect, posterRect)) {
      warnings.push({
        code: 'element_outside_poster', poster_id: posterId,
        message: 'A poster element extends beyond the physical page.'
      });
    }
    const elementStyle = window.getComputedStyle(element);
    const ownsOverflowBoundary = (
      element !== poster &&
      [elementStyle.overflowX, elementStyle.overflowY]
        .some((value) => value !== 'visible')
    );
    if (
      isVisible && ownsOverflowBoundary &&
      (element.scrollWidth > element.clientWidth + 1 ||
       element.scrollHeight > element.clientHeight + 1)
    ) {
      warnings.push({
        code: 'element_content_overflow', poster_id: posterId,
        observed: {
          overflow_x_px: Math.max(0, element.scrollWidth - element.clientWidth),
          overflow_y_px: Math.max(0, element.scrollHeight - element.clientHeight)
        },
        message: 'Poster element content exceeds its rendered box.'
      });
    }
    const clipReason = isVisible ? clippingReason(element, rect) : null;
    if (clipReason) {
      warnings.push({
        code: (
          clipReason === 'overflow' && element.hasAttribute('data-poster-module')
            ? 'module_clipped_by_ancestor'
            : 'ancestor_clips_element'
        ),
        poster_id: posterId,
        message: 'An ancestor clips part of this poster element.'
      });
    }
    return {poster_id: posterId, visible: isVisible, content, rect: relativeRect(rect)};
  });

  const moduleElements = Array.from(document.querySelectorAll('[data-poster-module]'));
  const modules = moduleElements.map((element) => {
    const rect = element.getBoundingClientRect();
    const moduleId = (element.getAttribute('data-poster-module') || '').trim();
    const style = getComputedStyle(element);
    const parent = element.parentElement;
    const parentStyle = parent ? getComputedStyle(parent) : null;
    return {
      module_id: moduleId,
      poster_id: (element.getAttribute('data-poster-id') || moduleId).trim(),
      visible: visible(element),
      rect: relativeRect(rect),
      layout: {
        parent_tag: parent?.tagName.toLowerCase() || '',
        parent_id: parent?.id || '',
        parent_classes: parent ? Array.from(parent.classList) : [],
        parent_poster_id: (
          parent?.getAttribute('data-poster-id') ||
          parent?.getAttribute('data-poster-module') || ''
        ).trim(),
        parent_rect: parent ? relativeRect(parent.getBoundingClientRect()) : null,
        parent_display: parentStyle?.display || '',
        parent_paints_bottom: paintsBottom(parentStyle),
        parent_flex_direction: parentStyle?.flexDirection || '',
        parent_flex_wrap: parentStyle?.flexWrap || '',
        parent_align_content: parentStyle?.alignContent || '',
        parent_grid_template_columns: parentStyle?.gridTemplateColumns || '',
        parent_grid_template_rows: parentStyle?.gridTemplateRows || '',
        parent_gap: parentStyle?.gap || '',
        grid_row_start: style.gridRowStart || '',
        grid_row_end: style.gridRowEnd || '',
        grid_column_start: style.gridColumnStart || '',
        grid_column_end: style.gridColumnEnd || '',
        flex_order: style.order || '',
        flex_grow: style.flexGrow,
        flex_shrink: style.flexShrink,
        flex_basis: style.flexBasis,
        min_height: style.minHeight,
        overflow_x: style.overflowX,
        overflow_y: style.overflowY,
        ancestor_chain: layoutAncestorChain(element)
      }
    };
  }).filter((item) => item.visible && item.rect.width > 0 && item.rect.height > 0);
  const titleElement = poster.querySelector('[data-poster-title-band]');
  const titleBand = titleElement && visible(titleElement)
    ? {rect: relativeRect(titleElement.getBoundingClientRect())}
    : null;

  const paintedImageSize = __PAINTED_IMAGE_SIZE__;
  const sourceDetails = Array.from(
    poster.querySelectorAll('img[data-source-figure-sha256]')
  ).map((image) => {
    const module = image.closest('[data-poster-module]');
    const figure = image.closest('figure');
    const group = figure?.parentElement || null;
    const figureRect = figure?.getBoundingClientRect() || image.getBoundingClientRect();
    const moduleRect = module?.getBoundingClientRect() || image.getBoundingClientRect();
    const groupStyle = group ? getComputedStyle(group) : null;
    const imageStyle = getComputedStyle(image);
    const groupSourceFigureCount = group
      ? Array.from(group.children).filter(
          (child) => child.matches('figure') &&
            child.querySelector('img[data-source-figure-sha256]')
        ).length
      : 1;
    const sha256 = (image.getAttribute('data-source-figure-sha256') || '').trim().toLowerCase();
    const rect = image.getBoundingClientRect();
    const hashValid = /^[0-9a-f]{64}$/.test(sha256);
    const isVisible = visible(image);
    const rendered = (
      image.complete && image.naturalWidth > 0 && image.naturalHeight > 0 &&
      rect.width > 0 && rect.height > 0
    );
    const declaredAspect = Number(image.dataset.sourceFigureAspectRatio);
    const sourceAspect = Number.isFinite(declaredAspect) && declaredAspect > 0
      ? declaredAspect
      : rendered ? image.naturalWidth / image.naturalHeight : 0;
    const renderedAspect = rendered ? rect.width / rect.height : 0;
    const aspectChanged = (
      rendered && sourceAspect > 0 &&
      Math.abs(Math.log(renderedAspect / sourceAspect)) > 0.02
    );
    const sourcePixelsAltered = (
      aspectChanged && !['contain', 'scale-down'].includes(imageStyle.objectFit)
    );
    const isClipped = clipped(image, rect);
    const isInside = inside(rect, posterRect);
    const paintedSize = rendered
      ? paintedImageSize(image, rect)
      : {width: rect.width, height: rect.height};
    const paintedWidthMm = paintedSize.width * pxToMm;
    const usable = (
      hashValid && isVisible && rendered && !isClipped && isInside
    );
    const paintedArea = paintedSize.width * paintedSize.height;
    const areaRatio = posterArea > 0 ? paintedArea / posterArea : 0;
    if (!hashValid) warnings.push({
      code: 'invalid_source_figure_hash', sha256,
      message: 'A source figure has an invalid SHA-256 identity.'
    });
    if (hashValid && !isVisible) warnings.push({
      code: 'source_figure_hidden', sha256,
      message: 'A selected source figure is hidden.'
    });
    if (hashValid && !rendered) warnings.push({
      code: 'source_figure_not_rendered', sha256,
      message: 'A selected source figure has no nonzero rendered image.'
    });
    if (hashValid && isClipped) warnings.push({
      code: 'source_figure_clipped', sha256,
      message: 'A selected source figure is clipped.'
    });
    if (hashValid && !isInside) warnings.push({
      code: 'source_figure_outside_poster', sha256,
      message: 'A selected source figure extends outside the poster.'
    });
    if (hashValid && sourcePixelsAltered) warnings.push({
      code: 'source_figure_cropped_or_distorted', sha256,
      observed: {
        object_fit: imageStyle.objectFit,
        source_aspect_ratio: sourceAspect,
        rendered_box_aspect_ratio: renderedAspect
      },
      message: 'A selected source figure is cropped or distorted instead of preserving its intact aspect ratio.'
    });
    return {
      sha256, hash_valid: hashValid, visible: isVisible, rendered, usable,
      module_id: (module?.getAttribute('data-poster-module') || '').trim(),
      module_priority: (module?.getAttribute('data-module-priority') || '').trim(),
      clipped: isClipped,
      source_pixels_altered: sourcePixelsAltered,
      inside_poster: isInside,
      rect: relativeRect(rect),
      painted_rect: relativeRect({
        left: rect.left + (rect.width - paintedSize.width) / 2,
        top: rect.top + (rect.height - paintedSize.height) / 2,
        width: paintedSize.width,
        height: paintedSize.height
      }),
      painted_width_mm: paintedWidthMm,
      figure_wrapper_width_mm: figureRect.width * pxToMm,
      module_width_mm: moduleRect.width * pxToMm,
      group_source_figure_count: groupSourceFigureCount,
      group_display: groupStyle?.display || '',
      group_flex_direction: groupStyle?.flexDirection || '',
      image_computed_style: {
        width: imageStyle.width,
        height: imageStyle.height,
        max_width: imageStyle.maxWidth,
        max_height: imageStyle.maxHeight,
        object_fit: imageStyle.objectFit,
      },
      area_ratio: areaRatio,
      width_ratio: posterRect.width > 0 ? paintedSize.width / posterRect.width : 0,
      height_ratio: posterRect.height > 0 ? paintedSize.height / posterRect.height : 0
    };
  });
  const validSourceDetails = sourceDetails.filter((item) => /^[0-9a-f]{64}$/.test(item.sha256));
  const sourceFigures = {
    count: sourceDetails.length,
    visible_count: validSourceDetails.filter((item) => item.visible).length,
    rendered_count: validSourceDetails.filter((item) => item.rendered).length,
    usable_count: validSourceDetails.filter((item) => item.usable).length,
    referenced_sha256s: Array.from(new Set(validSourceDetails.map((item) => item.sha256))),
    visible_sha256s: Array.from(new Set(
      validSourceDetails.filter((item) => item.visible).map((item) => item.sha256)
    )),
    usable_sha256s: Array.from(new Set(
      validSourceDetails.filter((item) => item.usable).map((item) => item.sha256)
    )),
    details: sourceDetails
  };

  const hasDirectText = (element) => Array.from(element.childNodes).some(
    (node) => node.nodeType === Node.TEXT_NODE && (node.textContent || '').trim()
  );
  function textEvidence(element) {
    const nodes = element.matches('math')
      ? [element]
      : Array.from(element.childNodes).filter(
        (node) => node.nodeType === Node.TEXT_NODE && (node.textContent || '').trim()
      );
    const rects = [];
    let charCount = 0;
    for (const node of nodes) {
      const text = (node.textContent || '').replace(/\s+/g, ' ').trim();
      if (!text) continue;
      charCount += text.length;
      const range = document.createRange();
      if (node === element) range.selectNodeContents(element);
      else range.selectNodeContents(node);
      for (const rect of range.getClientRects()) {
        if (rect.width > 0 && rect.height > 0) rects.push(relativeRect(rect));
      }
    }
    return {charCount, rects};
  }
  function renderedLineTexts(element) {
    const lines = [];
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let pendingSpace = false;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.textContent || '';
      for (let offset = 0; offset < text.length;) {
        const codePoint = text.codePointAt(offset);
        if (codePoint === undefined) break;
        const character = String.fromCodePoint(codePoint);
        const nextOffset = offset + character.length;
        if (/\s/u.test(character)) {
          pendingSpace = true;
          offset = nextOffset;
          continue;
        }
        const range = document.createRange();
        range.setStart(node, offset);
        range.setEnd(node, nextOffset);
        const rect = Array.from(range.getClientRects()).find(
          (value) => value.width > 0 && value.height > 0
        );
        offset = nextOffset;
        if (!rect) continue;
        let line = lines.find(
          (value) => Math.abs(value.top - rect.top) <= Math.max(2, rect.height * 0.2)
        );
        if (!line) {
          line = {top: rect.top, text: ''};
          lines.push(line);
        }
        if (pendingSpace && line.text) line.text += ' ';
        line.text += character;
        pendingSpace = false;
      }
    }
    return lines
      .sort((left, right) => left.top - right.top)
      .map((line) => line.text);
  }
  function backgroundLayers(element) {
    const layers = [];
    for (let current = element; current; current = current.parentElement) {
      const color = window.getComputedStyle(current).backgroundColor;
      if (
        color && color !== 'transparent' &&
        color !== 'rgba(0, 0, 0, 0)' && color !== 'rgba(0,0,0,0)'
      ) layers.push(color);
      if (current === poster) break;
    }
    return layers;
  }
  const titleContentRects = [];
  const typeNodes = [poster, ...poster.querySelectorAll('*')].filter((element) => (
    element.matches('math') || (!element.closest('math') && hasDirectText(element))
  ));
  const typography = typeNodes.filter((element) => visible(element)).map((element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    const evidence = textEvidence(element);
    if (titleElement?.contains(element)) titleContentRects.push(...evidence.rects);
    const moduleId = (
      element.closest('[data-poster-module]')?.getAttribute('data-poster-module') || ''
    ).trim();
    const modulePriority = (
      element.closest('[data-poster-module]')?.getAttribute('data-module-priority') || ''
    ).trim();
    const role = element.closest('[data-poster-title-band]') && element.closest('h1')
      ? 'title'
      : element.matches('math[data-content-role="equation"]')
      ? 'equation'
      : element.closest(
          '[data-poster-venue="verified"],[data-poster-affiliations="verified"]'
        )
      ? 'identity'
      : element.closest(
          '.section-label,.source-label,.equation-label,[data-content-role="source-label"]'
        )
      ? 'provenance'
      : element.closest('figcaption,[data-content-role="caption"],.fig-caption')
      ? 'caption'
      : element.closest('[data-module-priority="focal"]') &&
        element.matches('h2,h3,[data-content-role="takeaway"]')
      ? 'focal'
      : element.matches('h2,h3')
      ? 'heading'
      : element.closest('[data-content-role="provenance"]') ||
        element.closest('[data-semantic-roles~="provenance"]')
      ? 'provenance'
      : 'body';
    const fontSizePx = Number.parseFloat(style.fontSize) || 0;
    const lineHeightPx = Number.parseFloat(style.lineHeight) || fontSizePx * 1.2;
    return {
      poster_id: (
        element.closest('[data-poster-id]')?.getAttribute('data-poster-id') || moduleId
      ).trim(),
      module_id: moduleId,
      module_priority: modulePriority,
      role,
      element_tag: element.tagName.toLowerCase(),
      element: {tag: element.tagName.toLowerCase(), id: element.id || '', classes: Array.from(element.classList)},
      text: Array.from(element.childNodes).filter((node) => node.nodeType === Node.TEXT_NODE)
        .map((node) => node.textContent).join(' ').replace(/\s+/g, ' ').trim(),
      char_count: evidence.charCount,
      rects: evidence.rects,
      element_rect: relativeRect(rect),
      font_size_px: fontSizePx,
      font_size_mm: fontSizePx * pxToMm,
      font_family: style.fontFamily,
      font_weight: style.fontWeight,
      line_height_px: lineHeightPx,
      letter_spacing_px: Number.parseFloat(style.letterSpacing) || 0,
      foreground_color: style.color,
      background_layers: backgroundLayers(element),
      text_align: style.textAlign,
      line_count: lineHeightPx > 0 ? Math.max(1, Math.round(rect.height / lineHeightPx)) : 1,
      ...(role === 'title' ? {line_texts: renderedLineTexts(element)} : {})
    };
  });
  const textRuns = typography.filter((item) => item.char_count > 0 && item.rects.length > 0);
  const rectRight = (rect) => rect.left + rect.width;
  const rectBottom = (rect) => rect.top + rect.height;
  function collisionRelation(sourceRect, textRect) {
    const sourceContainsText = (
      textRect.left >= sourceRect.left && textRect.top >= sourceRect.top &&
      rectRight(textRect) <= rectRight(sourceRect) &&
      rectBottom(textRect) <= rectBottom(sourceRect)
    );
    if (sourceContainsText) return 'text_inside_source';
    const textContainsSource = (
      sourceRect.left >= textRect.left && sourceRect.top >= textRect.top &&
      rectRight(sourceRect) <= rectRight(textRect) &&
      rectBottom(sourceRect) <= rectBottom(textRect)
    );
    if (textContainsSource) return 'source_inside_text';
    const crossedEdges = [];
    if (textRect.top < sourceRect.top && rectBottom(textRect) > sourceRect.top) {
      crossedEdges.push({
        relation: 'text_crosses_source_top',
        penetration: rectBottom(textRect) - sourceRect.top
      });
    }
    if (
      textRect.top < rectBottom(sourceRect) &&
      rectBottom(textRect) > rectBottom(sourceRect)
    ) {
      crossedEdges.push({
        relation: 'text_crosses_source_bottom',
        penetration: rectBottom(sourceRect) - textRect.top
      });
    }
    if (textRect.left < sourceRect.left && rectRight(textRect) > sourceRect.left) {
      crossedEdges.push({
        relation: 'text_crosses_source_left',
        penetration: rectRight(textRect) - sourceRect.left
      });
    }
    if (
      textRect.left < rectRight(sourceRect) &&
      rectRight(textRect) > rectRight(sourceRect)
    ) {
      crossedEdges.push({
        relation: 'text_crosses_source_right',
        penetration: rectRight(sourceRect) - textRect.left
      });
    }
    crossedEdges.sort((left, right) => left.penetration - right.penetration);
    return crossedEdges[0]?.relation || 'intersecting';
  }
  for (const source of sourceDetails) {
    if (!source.visible || !source.rendered || !source.module_id) continue;
    let largestOverlap = null;
    for (const textRun of textRuns) {
      if (textRun.module_id !== source.module_id) continue;
      for (const textRect of textRun.rects) {
        const overlapWidth = Math.min(rectRight(source.rect), rectRight(textRect))
          - Math.max(source.rect.left, textRect.left);
        const overlapHeight = Math.min(rectBottom(source.rect), rectBottom(textRect))
          - Math.max(source.rect.top, textRect.top);
        if (overlapWidth <= 1 || overlapHeight <= 1) continue;
        if (
          !largestOverlap ||
          overlapWidth * overlapHeight >
            largestOverlap.overlap_width_px * largestOverlap.overlap_height_px
        ) {
          largestOverlap = {
            overlap_width_px: overlapWidth,
            overlap_height_px: overlapHeight,
            text_role: textRun.role,
            text_poster_id: textRun.poster_id,
            text_element_tag: textRun.element_tag,
            collision_relation: collisionRelation(source.rect, textRect),
            source_rect: source.rect,
            text_line_rect: textRect,
            text_element_rect: textRun.element_rect
          };
        }
      }
    }
    if (largestOverlap) warnings.push({
      code: 'source_figure_text_overlap',
      poster_id: source.module_id,
      sha256: source.sha256,
      observed: largestOverlap,
      message: 'A selected source figure overlaps editable poster text.'
    });
  }
  for (const table of poster.querySelectorAll('table')) {
    if (!visible(table)) continue;
    const module = table.closest('[data-poster-module]');
    if (!module) continue;
    const tableRect = relativeRect(table.getBoundingClientRect());
    const neighbors = module.querySelectorAll(
      'h2,h3,h4,figcaption,.fig-caption,[data-content-role="caption"]'
    );
    for (const neighbor of neighbors) {
      if (table.contains(neighbor) || !visible(neighbor)) continue;
      const textRect = relativeRect(neighbor.getBoundingClientRect());
      const overlapWidth = Math.min(rectRight(tableRect), rectRight(textRect))
        - Math.max(tableRect.left, textRect.left);
      const overlapHeight = Math.min(rectBottom(tableRect), rectBottom(textRect))
        - Math.max(tableRect.top, textRect.top);
      if (overlapWidth <= 1 || overlapHeight <= 1) continue;
      warnings.push({
        code: 'table_text_overlap',
        poster_id: (module.getAttribute('data-poster-id') ||
          module.getAttribute('data-poster-module') || '').trim(),
        observed: {
          overlap_width_px: overlapWidth,
          overlap_height_px: overlapHeight,
          table_rect: tableRect,
          text_rect: textRect,
          text_element_tag: neighbor.tagName.toLowerCase()
        },
        message: 'A native table overlaps its heading or caption text.'
      });
    }
  }
  const visualLeafTags = new Set(['img', 'svg', 'canvas', 'table', 'math']);
  const visualSelector = [
    ...visualLeafTags, 'pre', 'figure', '[data-module-priority="focal"]'
  ].join(',');
  const visualTargets = Array.from(poster.querySelectorAll(visualSelector))
    .filter((element) => visible(element))
    .map((element, index) => {
      const rect = element.getBoundingClientRect();
      if (
        titleElement?.contains(element) && visualLeafTags.has(element.tagName.toLowerCase())
      ) titleContentRects.push(relativeRect(rect));
      const moduleId = (
        element.closest('[data-poster-module]')?.getAttribute('data-poster-module') || ''
      ).trim();
      const posterId = (
        element.closest('[data-poster-id]')?.getAttribute('data-poster-id') || moduleId
      ).trim();
      return {
        target_id: `${element.tagName.toLowerCase()}:${posterId || moduleId || 'anonymous'}:${index}`,
        kind: element.tagName.toLowerCase(),
        poster_id: posterId,
        module_id: moduleId,
        rect: relativeRect(rect)
      };
    })
    .filter((item) => item.rect.width > 0 && item.rect.height > 0);

  const textRects = textRuns.flatMap((item) => item.rects.map((rect) => ({
    module_id: item.module_id,
    rect,
    element: item.element,
    text: item.text,
    element_rect: item.element_rect
  })));
  const contentRects = [
    ...textRects,
    ...visualTargets.map((item) => ({module_id: item.module_id, rect: item.rect}))
  ];
  const collisionRects = [
    ...textRects,
    ...visualTargets.filter((item) => visualLeafTags.has(item.kind))
      .map((item) => ({module_id: item.module_id, rect: item.rect}))
  ];
  const collisionRegions = modules.map((module) => ({
    id: module.module_id,
    box: module.rect,
    content: collisionRects.filter((item) => item.module_id === module.module_id)
      .map((item) => item.rect)
  }));
  if (titleBand) collisionRegions.push({
    id: titleElement.getAttribute('data-poster-id') || 'title-band',
    box: null,
    content: titleContentRects
  });
  for (let first = 0; first < collisionRegions.length; first += 1) {
    const a = collisionRegions[first];
    for (const b of collisionRegions.slice(first + 1)) {
      // Scientific modules keep their independent box boundary. A masthead can
      // share blank area with the body; only its rendered content can collide.
      const boxesA = a.box && b.box ? [a.box] : a.content;
      const boxesB = a.box && b.box ? [b.box] : b.content;
      let overlap = null;
      for (const rectA of boxesA) {
        for (const rectB of boxesB) {
          const width = Math.min(rectRight(rectA), rectRight(rectB))
            - Math.max(rectA.left, rectB.left);
          const height = Math.min(rectBottom(rectA), rectBottom(rectB))
            - Math.max(rectA.top, rectB.top);
          if (width > 1 && height > 1) {
            overlap = {module_a: a.id, module_b: b.id,
              overlap_width_px: width, overlap_height_px: height};
            break;
          }
        }
        if (overlap) break;
      }
      if (overlap) warnings.push({
        code: 'module_overlap', observed: overlap,
        message: 'Independent poster regions overlap in the rendered composition.'
      });
    }
  }
  const right = rectRight;
  const bottom = rectBottom;
  const insideBox = (inner, outer, tolerance = 1) => (
    inner.left >= outer.left - tolerance &&
    inner.top >= outer.top - tolerance &&
    right(inner) <= right(outer) + tolerance &&
    bottom(inner) <= bottom(outer) + tolerance
  );
  const intersects = (first, second, tolerance = 1) => (
    Math.min(right(first), right(second)) - Math.max(first.left, second.left) > tolerance &&
    Math.min(bottom(first), bottom(second)) - Math.max(first.top, second.top) > tolerance
  );
  const posterBox = {left: 0, top: 0, width: posterRect.width, height: posterRect.height};
  // The page contains rendered text/media, not invisible layout scroll extents.
  const pageStyle = getComputedStyle(poster);
  const pageContentBox = {
    left: pageStyle.overflowX === 'visible' ? 0 : poster.clientLeft,
    top: pageStyle.overflowY === 'visible' ? 0 : poster.clientTop,
    width: pageStyle.overflowX === 'visible' ? posterRect.width : poster.clientWidth,
    height: pageStyle.overflowY === 'visible' ? posterRect.height : poster.clientHeight
  };
  const containmentRegions = [
    {module_id: null, poster_id: poster.getAttribute('data-poster-id'), rect: pageContentBox},
    ...modules
  ];
  for (const module of containmentRegions) {
    const isPage = module.module_id === null;
    const outside = (isPage ? collisionRects : contentRects).filter((item) => (
      (isPage || item.module_id === module.module_id) && !insideBox(item.rect, module.rect)
    ));
    if (!outside.length) continue;
    const overlappingModuleIds = modules
      .filter((other) => (
        other.module_id !== module.module_id &&
        outside.some((item) => intersects(item.rect, other.rect))
      ))
      .map((other) => other.module_id);
    const outsidePoster = outside.some((item) => !insideBox(item.rect, posterBox));
    if (!isPage && !outsidePoster && !overlappingModuleIds.length) continue;
    if (warnings.some((item) => (
      item.code === 'element_content_overflow' && item.poster_id === module.poster_id
    ))) continue;
    warnings.push({
      code: 'element_content_overflow',
      poster_id: module.poster_id,
      observed: {
        overflow_x_px: Math.max(...outside.map((item) => (
          Math.max(0, module.rect.left - item.rect.left) +
          Math.max(0, right(item.rect) - right(module.rect))
        ))),
        overflow_y_px: Math.max(...outside.map((item) => (
          Math.max(0, module.rect.top - item.rect.top) +
          Math.max(0, bottom(item.rect) - bottom(module.rect))
        ))),
        overlapping_module_ids: overlappingModuleIds,
        outside_poster: outsidePoster,
        overflowing_content: outside.map(({module_id, ...item}) => item)
      },
      message: 'Poster element content exceeds its rendered box and is obscured or clipped.'
    });
  }

  // CSSOM and selector matching belong to the browser, not a second CSS parser.
  // A zero match is editor context only: unused rules may be intentional.
  const unmatchedStyleSelectors = new Set();
  function collectUnmatchedStyleSelectors(rules) {
    for (const rule of rules) {
      if (rule.selectorText) {
        try {
          if (!document.querySelector(rule.selectorText)) {
            unmatchedStyleSelectors.add(rule.selectorText);
          }
        } catch (_) {
          // Some valid stylesheet selectors (such as nested &) need CSS context.
        }
      }
      if (rule.cssRules) collectUnmatchedStyleSelectors(rule.cssRules);
    }
  }
  for (const sheet of document.styleSheets) {
    collectUnmatchedStyleSelectors(sheet.cssRules);
  }

  return {
    poster: {
      width: posterRect.width,
      height: posterRect.height,
      width_mm: posterRect.width * pxToMm,
      height_mm: posterRect.height * pxToMm
    },
    modules,
    title_band: titleBand,
    typography,
    text_runs: textRuns,
    visual_targets: visualTargets,
    source_figures: sourceFigures,
    unmatched_style_selectors: [...unmatchedStyleSelectors],
    elements,
    warnings
  };
}
