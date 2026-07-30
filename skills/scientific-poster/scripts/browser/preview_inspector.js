(() => {
  const token = __POSTER_PREVIEW_TOKEN_JSON__;
  const sourceHash = __POSTER_SOURCE_HASH_JSON__;
  const highlight = document.getElementById(__POSTER_HIGHLIGHT_ID_JSON__);
  const panel = document.getElementById(__POSTER_PANEL_ID_JSON__);
  const label = panel.querySelector('span');
  let selectedId = '';
  const styleNames = ['font-family','font-size','font-style','font-weight','line-height',
    'letter-spacing','color','background-color','display','position','grid-template-columns',
    'grid-template-rows','gap','align-items','justify-content','text-align','overflow',
    'border-radius','opacity','transform'];
  const targetFor = event => event.target instanceof Element
    ? event.target.closest('[data-poster-id]') : null;
  const show = element => {
    if (!element) { highlight.hidden = true; return; }
    const rect = element.getBoundingClientRect();
    Object.assign(highlight.style, {left:`${rect.left}px`,top:`${rect.top}px`,
      width:`${rect.width}px`,height:`${rect.height}px`});
    highlight.hidden = false;
  };
  document.addEventListener('mousemove', event => {
    if (!panel.contains(event.target)) show(targetFor(event));
  }, true);
  document.addEventListener('click', async event => {
    if (panel.contains(event.target)) return;
    const element = targetFor(event); if (!element) return;
    event.preventDefault(); event.stopPropagation(); show(element);
    const module = element.closest('[data-poster-module]');
    const rect = element.getBoundingClientRect();
    const computed = getComputedStyle(element); const styles = {};
    for (const name of styleNames) styles[name] = computed.getPropertyValue(name).slice(0,300);
    selectedId = element.dataset.posterId || '';
    label.textContent = `Selected: ${selectedId}`;
    const payload = {source_html_sha256:sourceHash,poster_id:selectedId,
      poster_module:module?.dataset.posterModule || '',
      semantic_roles:module?.dataset.semanticRoles || '',
      module_priority:module?.dataset.modulePriority || '',
      text_sample:(element.innerText || '').replace(/\s+/g,' ').trim().slice(0,500),
      rect:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},styles};
    try {
      const response = await fetch(`/__poster_selection?token=${encodeURIComponent(token)}`, {
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload),credentials:'same-origin'
      });
      if (!response.ok) label.textContent = response.status === 409
        ? 'Poster changed; reloading…' : `Selection failed (${response.status})`;
    } catch (_) { label.textContent = 'Selection could not be saved'; }
  }, true);
  panel.querySelector('button').addEventListener('click', async () => {
    if (!selectedId) return;
    try { await navigator.clipboard.writeText(selectedId); } catch (_) {}
  });
  let polling = false;
  setInterval(async () => {
    if (polling) return; polling = true;
    try {
      const response = await fetch(`/__poster_state?token=${encodeURIComponent(token)}`,
        {cache:'no-store',credentials:'same-origin'});
      if (response.ok && (await response.json()).source_html_sha256 !== sourceHash)
        window.location.reload();
    } catch (_) {} finally { polling = false; }
  }, 750);
})();
