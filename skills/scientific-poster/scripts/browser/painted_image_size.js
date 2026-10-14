(image, rect) => {
  const objectFit = window.getComputedStyle(image).objectFit;
  if (!['contain', 'scale-down'].includes(objectFit)) {
    return {width: rect.width, height: rect.height};
  }
  const declaredAspect = Number(image.dataset.sourceFigureAspectRatio);
  const sourceAspect = Number.isFinite(declaredAspect) && declaredAspect > 0
    ? declaredAspect : image.naturalWidth / image.naturalHeight;
  const intrinsicWidth = sourceAspect * image.naturalHeight;
  const containScale = Math.min(
    rect.width / intrinsicWidth,
    rect.height / image.naturalHeight
  );
  const scale = objectFit === 'scale-down' ? Math.min(1, containScale) : containScale;
  return {
    width: intrinsicWidth * scale,
    height: image.naturalHeight * scale
  };
}
