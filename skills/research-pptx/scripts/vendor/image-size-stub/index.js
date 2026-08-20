"use strict";

// pptxgenjs still lists image-size, but the shipped getSizeFromImage helper
// is unused (commented FIXME). CVE-2025-71329 / CVE-2025-71330 have no
// patched release (<= 2.0.2 is latest). This stub removes the parsers.
function imageSize() {
  throw new Error(
    "image-size is stubbed in research-pptx; the renderer passes explicit w/h"
  );
}

module.exports = imageSize;
module.exports.default = imageSize;
module.exports.imageSize = imageSize;
module.exports.types = [];
module.exports.disableTypes = function disableTypes() {};
module.exports.setConcurrency = function setConcurrency() {};
