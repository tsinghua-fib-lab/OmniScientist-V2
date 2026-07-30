async () => {
  if (document.fonts && document.fonts.ready) await document.fonts.ready;
  const images = Array.from(document.images);
  for (const image of images) image.loading = 'eager';
  await Promise.all(images.map(async (image) => {
    if (image.complete && image.naturalWidth > 0) return;
    try { await image.decode(); } catch (_) {
      await new Promise((resolve) => {
        image.addEventListener('load', resolve, {once: true});
        image.addEventListener('error', resolve, {once: true});
      });
    }
  }));
}
