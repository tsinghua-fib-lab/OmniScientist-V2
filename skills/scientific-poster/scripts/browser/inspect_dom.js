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
        readable_count: 0, referenced_sha256s: [], visible_sha256s: [],
        usable_sha256s: [], readable_sha256s: [], details: []
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
  if (poster.scrollWidth > poster.clientWidth + 1 || poster.scrollHeight > poster.clientHeight + 1) {
    warnings.push({
      code: 'poster_scroll_overflow',
      message: 'Poster content exceeds the poster box.'
    });
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

  function clipped(element, rect) {
    for (let current = element.parentElement; current; current = current.parentElement) {
      const style = window.getComputedStyle(current);
      if (
        (style.clipPath && style.clipPath !== 'none') ||
        (style.maskImage && style.maskImage !== 'none') ||
        (style.webkitMaskImage && style.webkitMaskImage !== 'none')
      ) return true;
      const overflowClips = [style.overflow, style.overflowX, style.overflowY]
        .some((value) => value === 'hidden' || value === 'clip');
      if (overflowClips && !inside(rect, current.getBoundingClientRect())) return true;
      if (current === poster) break;
    }
    return false;
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

  const elements = Array.from(document.querySelectorAll('[data-poster-id]')).map((element) => {
    const posterId = (element.getAttribute('data-poster-id') || '').trim();
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
    if (
      isVisible &&
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
    if (isVisible && clipped(element, rect)) {
      warnings.push({
        code: 'ancestor_clips_element', poster_id: posterId,
        message: 'An ancestor clips part of this poster element.'
      });
    }
    return {poster_id: posterId, visible: isVisible, content, rect: relativeRect(rect)};
  });

  const modules = Array.from(document.querySelectorAll('[data-poster-module]')).map((element) => {
    const rect = element.getBoundingClientRect();
    return {
      module_id: (element.getAttribute('data-poster-module') || '').trim(),
      poster_id: (element.getAttribute('data-poster-id') || '').trim(),
      visible: visible(element),
      rect: relativeRect(rect)
    };
  }).filter((item) => item.visible && item.rect.width > 0 && item.rect.height > 0);
  for (let first = 0; first < modules.length; first += 1) {
    const a = modules[first];
    const aRight = a.rect.left + a.rect.width;
    const aBottom = a.rect.top + a.rect.height;
    for (let second = first + 1; second < modules.length; second += 1) {
      const b = modules[second];
      const overlapWidth = Math.min(aRight, b.rect.left + b.rect.width)
        - Math.max(a.rect.left, b.rect.left);
      const overlapHeight = Math.min(aBottom, b.rect.top + b.rect.height)
        - Math.max(a.rect.top, b.rect.top);
      if (overlapWidth > 1 && overlapHeight > 1) {
        warnings.push({
          code: 'module_overlap',
          observed: {
            module_a: a.module_id,
            module_b: b.module_id,
            overlap_width_px: overlapWidth,
            overlap_height_px: overlapHeight
          },
          message: 'Independent poster modules overlap in the rendered composition.'
        });
      }
    }
  }

  const titleElement = poster.querySelector('[data-poster-title-band]');
  const titleBand = titleElement && visible(titleElement)
    ? {rect: relativeRect(titleElement.getBoundingClientRect())}
    : null;

  const minimumFigureArea = posterArea * 0.005;
  const minimumLandscapeWidth = posterRect.width * 0.18;
  const minimumPortraitHeight = posterRect.height * 0.18;
  function paintedImageSize(image, rect) {
    const objectFit = window.getComputedStyle(image).objectFit;
    if (!['contain', 'scale-down'].includes(objectFit)) {
      return {width: rect.width, height: rect.height};
    }
    const containScale = Math.min(
      rect.width / image.naturalWidth,
      rect.height / image.naturalHeight
    );
    const scale = objectFit === 'scale-down' ? Math.min(1, containScale) : containScale;
    return {
      width: image.naturalWidth * scale,
      height: image.naturalHeight * scale
    };
  }
  const sourceDetails = Array.from(
    poster.querySelectorAll('img[data-source-figure-sha256]')
  ).map((image) => {
    const sha256 = (image.getAttribute('data-source-figure-sha256') || '').trim().toLowerCase();
    const rect = image.getBoundingClientRect();
    const hashValid = /^[0-9a-f]{64}$/.test(sha256);
    const isVisible = visible(image);
    const rendered = (
      image.complete && image.naturalWidth > 0 && image.naturalHeight > 0 &&
      rect.width > 0 && rect.height > 0
    );
    const isClipped = clipped(image, rect);
    const isInside = inside(rect, posterRect);
    const landscape = image.naturalWidth >= image.naturalHeight;
    const paintedSize = rendered
      ? paintedImageSize(image, rect)
      : {width: rect.width, height: rect.height};
    const readableExtent = landscape
      ? paintedSize.width + 1 >= minimumLandscapeWidth
      : paintedSize.height + 1 >= minimumPortraitHeight;
    const usable = (
      hashValid && isVisible && rendered && !isClipped && isInside
    );
    const paintedArea = paintedSize.width * paintedSize.height;
    const areaRatio = posterArea > 0 ? paintedArea / posterArea : 0;
    const readable = usable && paintedArea >= minimumFigureArea && readableExtent;
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
    return {
      sha256, hash_valid: hashValid, visible: isVisible, rendered, usable, readable,
      clipped: isClipped,
      inside_poster: isInside,
      rect: relativeRect(rect),
      painted_rect: relativeRect({
        left: rect.left + (rect.width - paintedSize.width) / 2,
        top: rect.top + (rect.height - paintedSize.height) / 2,
        width: paintedSize.width,
        height: paintedSize.height
      }),
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
    readable_count: validSourceDetails.filter((item) => item.readable).length,
    referenced_sha256s: Array.from(new Set(validSourceDetails.map((item) => item.sha256))),
    visible_sha256s: Array.from(new Set(
      validSourceDetails.filter((item) => item.visible).map((item) => item.sha256)
    )),
    usable_sha256s: Array.from(new Set(
      validSourceDetails.filter((item) => item.usable).map((item) => item.sha256)
    )),
    readable_sha256s: Array.from(new Set(
      validSourceDetails.filter((item) => item.readable).map((item) => item.sha256)
    )),
    minimum_area_ratio: 0.005,
    minimum_landscape_width_ratio: 0.18,
    minimum_portrait_height_ratio: 0.18,
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
  const typeNodes = [poster, ...poster.querySelectorAll('*')].filter((element) => (
    element.matches('math') || (!element.closest('math') && hasDirectText(element))
  ));
  const typography = typeNodes.filter((element) => visible(element)).map((element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    const evidence = textEvidence(element);
    const moduleId = (
      element.closest('[data-poster-module]')?.getAttribute('data-poster-module') || ''
    ).trim();
    const role = element.closest('[data-poster-title-band]') && element.closest('h1')
      ? 'title'
      : element.closest('[data-poster-venue="verified"]')
      ? 'identity'
      : element.closest('figcaption,[data-content-role="caption"],.fig-caption')
      ? 'caption'
      : element.closest('[data-focal-role]')
      ? 'focal'
      : element.closest('[data-content-role="provenance"]') ||
        element.closest('[data-semantic-roles~="provenance"]')
      ? 'provenance'
      : 'body';
    const fontSizePx = Number.parseFloat(style.fontSize) || 0;
    const lineHeightPx = Number.parseFloat(style.lineHeight) || fontSizePx * 1.2;
    return {
      poster_id: (element.closest('[data-poster-id]')?.getAttribute('data-poster-id') || '').trim(),
      module_id: moduleId,
      role,
      char_count: evidence.charCount,
      rects: evidence.rects,
      font_size_px: fontSizePx,
      font_size_mm: fontSizePx * pxToMm,
      font_family: style.fontFamily,
      line_count: lineHeightPx > 0 ? Math.max(1, Math.round(rect.height / lineHeightPx)) : 1
    };
  });
  const textRuns = typography.filter((item) => item.char_count > 0 && item.rects.length > 0);
  const visualSelector = [
    'img', 'svg', 'canvas', 'table', 'math', 'pre', 'figure', '[data-focal-role]'
  ].join(',');
  const visualTargets = Array.from(poster.querySelectorAll(visualSelector))
    .filter((element) => visible(element))
    .map((element, index) => {
      const rect = element.getBoundingClientRect();
      const moduleId = (
        element.closest('[data-poster-module]')?.getAttribute('data-poster-module') || ''
      ).trim();
      const posterId = (
        element.closest('[data-poster-id]')?.getAttribute('data-poster-id') || ''
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
    elements,
    warnings
  };
}
