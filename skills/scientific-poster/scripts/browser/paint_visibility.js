root => {
  const facts = globalThis.__posterPaintFacts;
  const posterBox = root.getBoundingClientRect();
  const intersectsPoster = box => box.width > 0 && box.height > 0 &&
    box.right > posterBox.left && box.left < posterBox.right &&
    box.bottom > posterBox.top && box.top < posterBox.bottom;
  const visible = (element, pseudo = '', requireBox = true) => {
    const style = getComputedStyle(element, pseudo ? `::${pseudo}` : null);
    if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return false;
    // A contents owner can generate a pseudo box. Chromium's snapshot supplies
    // that box independently of the owner's own border box.
    for (let current = element; current; current = current.parentElement) {
      const currentStyle = getComputedStyle(current);
      if (currentStyle.display === 'contents') continue;
      if (!current.checkVisibility({opacityProperty: true}) ||
          (currentStyle.contentVisibility === 'hidden' && (current !== element || pseudo))) return false;
    }
    if (!requireBox) return true;
    const boxes = pseudo ? (facts?.pseudoBoxes.get(element)?.[pseudo] || []).map(bounds => ({
      left: bounds[0] - scrollX, top: bounds[1] - scrollY,
      right: bounds[0] - scrollX + bounds[2], bottom: bounds[1] - scrollY + bounds[3],
      width: bounds[2], height: bounds[3],
    })) : [...element.getClientRects()];
    return boxes.some(intersectsPoster);
  };
  const backgroundImages = [];
  for (const element of [root, ...root.querySelectorAll('*')]) {
    for (const pseudo of ['', 'before', 'after']) {
      if (getComputedStyle(element, pseudo ? `::${pseudo}` : null).backgroundImage !== 'none' &&
          visible(element, pseudo)) backgroundImages.push({element, pseudo});
    }
  }
  for (const element of facts?.ancestorBackgrounds || []) backgroundImages.push({element, pseudo: ''});
  const textProbe = (text, candidates) => {
    const owner = text.parentElement;
    if (!visible(owner, '', false)) return null;
    const paints = candidates.filter(({element, pseudo}) => visible(element, pseudo));
    if (!paints.length) return null;
    const range = document.createRange();
    range.selectNodeContents(text);
    const bounds = range.getBoundingClientRect();
    const styles = new Map([owner, ...paints.filter(paint => !paint.pseudo).map(paint => paint.element)]
      .map(element => [element, element.getAttribute('style')]));
    const sheets = [...document.adoptedStyleSheets];
    const sheet = new CSSStyleSheet();
    const selector = element => {
      const parts = [];
      for (let current = element; current.parentElement; current = current.parentElement) {
        parts.unshift(`${CSS.escape(current.localName)}:nth-child(${[...current.parentElement.children].indexOf(current) + 1})`);
      }
      return [':root', ...parts].join(' > ');
    };
    const pseudoRules = paints.filter(paint => paint.pseudo)
      .map(({element, pseudo}) => `${selector(element)}::${pseudo}{opacity:0!important}`).join('\n');
    const restore = () => {
      for (const [element, style] of styles) {
        // Reset CSSOM through the DOM before restoring an absent attribute.
        element.setAttribute('style', style ?? '');
        if (style === null) {
          element.removeAttribute('style');
        }
      }
      document.adoptedStyleSheets = sheets;
    };
    return {
      rect: {x: bounds.x + scrollX, y: bounds.y + scrollY, width: bounds.width, height: bounds.height},
      restore,
      show(textVisible, paintVisible) {
        restore();
        if (!textVisible) {
          owner.style.setProperty('-webkit-text-fill-color', 'transparent', 'important');
          owner.style.setProperty('text-shadow', 'none', 'important');
          if (owner instanceof SVGElement) {
            owner.style.setProperty('fill', 'transparent', 'important');
            owner.style.setProperty('stroke', 'transparent', 'important');
          }
        }
        if (!paintVisible) {
          for (const {element, pseudo} of paints) {
            if (!pseudo) element.style.setProperty('opacity', '0', 'important');
          }
          sheet.replaceSync(pseudoRules);
          document.adoptedStyleSheets = [...sheets, sheet];
        }
      },
      record(evidence) {
        facts.textOcclusions.set(text, {...evidence, paints: paints.map(({element, pseudo, paint_order}) => ({
          element: {tag: element.localName, id: element.id, classes: [...element.classList]},
          module_id: element.closest('[data-poster-module]')?.getAttribute('data-poster-module') || '',
          poster_id: element.closest('[data-poster-id]')?.getAttribute('data-poster-id') || '',
          pseudo, paint_order
        }))});
      }
    };
  };
  return {visible, backgroundImages, textProbe};
}
