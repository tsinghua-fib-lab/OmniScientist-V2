() => { const root = document.querySelector(__POSTER_ROOT_SELECTOR__);
  if (!root) return null; const r = root.getBoundingClientRect();
  return {width: Math.ceil(r.width + 8), height: Math.ceil(r.height + 8)}; }
