(image, rect) => {
  const style = getComputedStyle(image);
  const inset = (side) => Number.parseFloat(style[`border${side}Width`]) +
    Number.parseFloat(style[`padding${side}`]);
  const leftInset = inset('Left'), topInset = inset('Top');
  const content = {
    left: rect.left + leftInset, top: rect.top + topInset,
    width: Math.max(0, rect.width - leftInset - inset('Right')),
    height: Math.max(0, rect.height - topInset - inset('Bottom')),
  };
  const objectFit = style.objectFit;
  let intrinsicWidth = image.naturalWidth, intrinsicHeight = image.naturalHeight;
  const source = image.currentSrc || image.getAttribute('src') || '';
  if (objectFit !== 'fill' && source.startsWith('data:image/svg+xml')) {
    // SVG naturalWidth/Height round to integers and supply a default viewport
    // for dimensionless resources. Browser layout retains the resource's ratio
    // and fractional dimensions without using author-supplied aspect metadata.
    const host = document.createElement('div');
    host.style.cssText = `all:initial!important;position:fixed!important;visibility:hidden!important;contain:strict!important;width:${content.width}px!important;height:${content.height}px!important;`;
    const probe = image.cloneNode();
    probe.style.cssText = 'all:initial!important;display:block!important;width:auto!important;height:auto!important;';
    host.attachShadow({mode: 'closed'}).append(probe);
    document.body.append(host);
    try {
      const natural = probe.getBoundingClientRect();
      host.style.setProperty('width', `${content.width * 2}px`, 'important');
      const expanded = probe.getBoundingClientRect();
      // A ratio-only SVG derives its default object size from the content box.
      const flexible = Math.abs(expanded.width - natural.width) > 0.01 ||
        Math.abs(expanded.height - natural.height) > 0.01;
      const scale = flexible ? Math.min(
        content.width / natural.width, content.height / natural.height,
      ) : 1;
      intrinsicWidth = natural.width * scale;
      intrinsicHeight = natural.height * scale;
    } finally {
      host.remove();
    }
  }
  let width = content.width, height = content.height;
  if (intrinsicWidth > 0 && intrinsicHeight > 0 && objectFit !== 'fill') {
    const scales = [content.width / intrinsicWidth, content.height / intrinsicHeight];
    const scale = objectFit === 'none' ? 1 : objectFit === 'cover' ? Math.max(...scales) :
      objectFit === 'scale-down' ? Math.min(1, ...scales) : Math.min(...scales);
    width = intrinsicWidth * scale;
    height = intrinsicHeight * scale;
  }
  // Chromium serializes edge keywords/offsets into two length-percentages.
  // Split only at top-level whitespace, preserving calc() and math functions.
  const position = style.objectPosition;
  let depth = 0, split = -1;
  for (let index = 0; index < position.length; index += 1) {
    const character = position[index];
    if (character === '(') depth += 1;
    else if (character === ')') depth -= 1;
    else if (depth === 0 && !character.trim()) { split = index; break; }
  }
  const components = [position.slice(0, split), position.slice(split + 1)];
  const offset = (value, space) => {
    try {
      return [...CSSNumericValue.parse(value).toSum('px', 'percent').values]
        .reduce((sum, term) => sum + term.value * (term.unit === 'percent' ? space / 100 : 1), 0);
    } catch (error) {
      // CSS min/max/clamp need a percentage basis before they can become pixels.
      // Let browser layout resolve them in an isolated, unpainted containing block.
      if (space < 0) throw error;
      const host = document.createElement('div');
      host.style.cssText = `all:initial!important;position:fixed!important;visibility:hidden!important;width:${space}px!important;height:0!important;`;
      const probe = document.createElement('div');
      probe.style.cssText = `position:absolute;left:${value};`;
      host.attachShadow({mode: 'closed'}).append(probe);
      document.body.append(host);
      try { return Number.parseFloat(getComputedStyle(probe).left); }
      finally { host.remove(); }
    }
  };
  const left = content.left + offset(components[0], content.width - width);
  const top = content.top + offset(components[1], content.height - height);
  return {
    left, top, width, height, right: left + width, bottom: top + height,
    clipped: left < content.left - 0.01 || top < content.top - 0.01 ||
      left + width > content.left + content.width + 0.01 ||
      top + height > content.top + content.height + 0.01,
  };
}
