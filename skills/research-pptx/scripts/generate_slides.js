#!/usr/bin/env node
/**
 * PptxGenJS renderer — reads slide_data.json, outputs .pptx
 *
 * Usage: node generate_slides.js <slide_data.json> <output.pptx>
 *
 * V2: Adaptive vertical distribution, accent bars, dynamic font sizing.
 */

const pptxgen = require("pptxgenjs");
const fs = require("fs");
const path = require("path");

let sharp;
try { sharp = require("sharp"); } catch (_) { sharp = null; }

// ============================================================
// CLI
// ============================================================
const dataPath = process.argv[2];
const outputPath = process.argv[3];

if (!dataPath || !outputPath) {
  console.error("Usage: node generate_slides.js <slide_data.json> <output.pptx>");
  process.exit(1);
}

const data = JSON.parse(fs.readFileSync(dataPath, "utf-8"));
const CONFIG = data.config;
const SLIDES = data.slides;

// ============================================================
// Layout Constants
// ============================================================
const SLIDE_W = 10;
const SLIDE_H = 5.625;
const MARGIN_TOP = 0.3;
const MARGIN_BOTTOM = 0.35;
const MARGIN_SIDE = 0.5;
const CONTENT_W = SLIDE_W - 2 * MARGIN_SIDE;
const GAP = 0.2;
const TITLE_H = 0.45;
const TITLE_FONT = 22;

const C = CONFIG.colors || {
  primary: "1E2761", secondary: "CADCFC", accent: "FFFFFF",
  dark: "0F1535", bodyText: "2D2D2D", muted: "6B7280",
  tableFill: "F0F4FF", tableHead: "1E2761",
};
const HEADER_FONT = CONFIG.headerFont || "Arial Black";
const BODY_FONT = CONFIG.bodyFont || "Arial";

// ============================================================
// Text Sanitization (defence against HTML entities in LLM output)
// ============================================================
const ENTITY_MAP = {
  "&#8226;": "•", "&#x2022;": "•", "&bull;": "•",
  "&#8211;": "–", "&ndash;": "–",
  "&#8212;": "—", "&mdash;": "—",
  "&#8216;": "‘", "&lsquo;": "‘",
  "&#8217;": "’", "&rsquo;": "’",
  "&#8220;": "“", "&ldquo;": "“",
  "&#8221;": "”", "&rdquo;": "”",
  "&#160;": " ", "&nbsp;": " ",
  "&#38;": "&", "&amp;": "&",
  "&#60;": "<", "&lt;": "<",
  "&#62;": ">", "&gt;": ">",
  "&quot;": '"', "&#34;": '"',
  "&#39;": "'", "&apos;": "'",
  "&times;": "×", "&plusmn;": "±",
  "&middot;": "·", "&#183;": "·",
  "&lsaquo;": "‹", "&rsaquo;": "›",
  "&#8208;": "‐", "&#8209;": "‑", "&#8213;": "―",
};
const ENTITY_RE = new RegExp(
  Object.keys(ENTITY_MAP)
    .sort((a, b) => b.length - a.length)
    .map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join("|"),
  "g",
);

function sanitizeText(text) {
  if (!text || typeof text !== "string") return text;
  return text.replace(ENTITY_RE, m => ENTITY_MAP[m]);
}

function sanitizeSlide(s) {
  if (s.title) s.title = sanitizeText(s.title);
  if (s.subtitle) s.subtitle = sanitizeText(s.subtitle);
  if (s.bullets) s.bullets = s.bullets.map(b => sanitizeText(b));
  if (s.figure_caption) s.figure_caption = sanitizeText(s.figure_caption);
  if (s.notes) s.notes = sanitizeText(s.notes);
  if (s.metrics) s.metrics = s.metrics.map(m => ({
    ...m,
    value: sanitizeText(m.value || ""),
    label: sanitizeText(m.label || ""),
  }));
  if (s.table_headers) s.table_headers = s.table_headers.map(h => sanitizeText(h));
  if (s.table_rows) s.table_rows = s.table_rows.map(r => r.map(c => sanitizeText(c)));
  if (s.extra) {
    // Two-column slides
    if (s.extra.columns) {
      s.extra.columns = s.extra.columns.map(col => ({
        ...col,
        sub_title: sanitizeText(col.sub_title || ""),
        bullets: (col.bullets || []).map(b => sanitizeText(b)),
        figure_caption: sanitizeText(col.figure_caption || ""),
      }));
    }
    // Icon rows
    if (s.extra.rows) {
      s.extra.rows = s.extra.rows.map(r => ({
        ...r,
        label: sanitizeText(r.label || ""),
        header: sanitizeText(r.header || ""),
        description: sanitizeText(r.description || ""),
      }));
    }
    // Steps
    if (s.extra.steps) {
      s.extra.steps = s.extra.steps.map(st => ({
        ...st,
        step_number: sanitizeText(st.step_number || ""),
        step_title: sanitizeText(st.step_title || ""),
        step_desc: sanitizeText(st.step_desc || ""),
      }));
    }
    // Emphasis box
    if (s.extra.box_text) s.extra.box_text = sanitizeText(s.extra.box_text);
    if (s.extra.emphasis_note) s.extra.emphasis_note = sanitizeText(s.extra.emphasis_note);
    if (s.extra.authors) s.extra.authors = sanitizeText(s.extra.authors);
    if (s.extra.affiliation) s.extra.affiliation = sanitizeText(s.extra.affiliation);
  }
  if (s.citations) {
    s.citations = s.citations.map(c => ({
      ...c,
      key: sanitizeText(c.key || ""),
      text: sanitizeText(c.text || ""),
    }));
  }
  return s;
}

// ============================================================
// Height Estimation (word-boundary wrapping)
// ============================================================
function _isCJK(ch) {
  const cp = ch.charCodeAt(0);
  return (
    (cp >= 0x4E00 && cp <= 0x9FFF) ||
    (cp >= 0x3400 && cp <= 0x4DBF) ||
    (cp >= 0xF900 && cp <= 0xFAFF) ||
    (cp >= 0xFF00 && cp <= 0xFFEF) ||
    (cp >= 0x3040 && cp <= 0x309F) ||
    (cp >= 0x30A0 && cp <= 0x30FF) ||
    (cp >= 0xAC00 && cp <= 0xD7AF)
  );
}

function estimateHeight(textItems, fontSizePt, boxWidthIn) {
  const latinCharW = fontSizePt * 0.55;
  const cjkCharW = fontSizePt * 1.0;
  const lineHeightPt = fontSizePt * 1.4;
  const usableWidthPt = boxWidthIn * 72;

  const paragraphs = (Array.isArray(textItems)
    ? textItems.map(t => (typeof t === "string" ? t : t.text || ""))
    : [String(textItems)]
  ).filter(Boolean);

  let totalLines = 0;
  for (const para of paragraphs) {
    if (!para.trim()) { totalLines += 1; continue; }

    // Detect if this paragraph is CJK-dominant
    let cjkCount = 0;
    for (const ch of para) {
      if (_isCJK(ch)) cjkCount++;
    }
    const isCjkDominant = cjkCount > para.length * 0.3;

    if (isCjkDominant) {
      // Character-based wrapping for CJK
      let lineWidth = 0;
      let lines = 1;
      for (const ch of para) {
        const chW = _isCJK(ch) ? cjkCharW : latinCharW;
        if (lineWidth + chW > usableWidthPt) {
          lines++;
          lineWidth = chW;
        } else {
          lineWidth += chW;
        }
      }
      totalLines += lines;
    } else {
      // Word-boundary wrapping for Latin
      const words = para.split(/\s+/);
      let lineWidth = 0;
      let lines = 1;
      for (const word of words) {
        let wordW = 0;
        for (const ch of word) {
          wordW += _isCJK(ch) ? cjkCharW : latinCharW;
        }
        const spaceW = latinCharW;
        if (lineWidth > 0 && lineWidth + spaceW + wordW > usableWidthPt) {
          lines++;
          lineWidth = wordW;
        } else {
          lineWidth += (lineWidth > 0 ? spaceW : 0) + wordW;
        }
      }
      totalLines += lines;
    }
  }
  return (totalLines * lineHeightPt) / 72 + 0.25;
}

// ============================================================
// Figure Embedding (aspect-ratio safe)
// ============================================================
async function fitImage(imgPath, maxW, maxH) {
  if (sharp) {
    try {
      const meta = await sharp(imgPath).metadata();
      const scale = Math.min(maxW / meta.width, maxH / meta.height);
      return { w: meta.width * scale, h: meta.height * scale };
    } catch (_) {}
  }
  const scale = Math.min(maxW / 1600, maxH / 1000);
  return { w: 1600 * scale, h: 1000 * scale };
}

// Shadow factory
const makeShadow = () => ({
  type: "outer", color: "000000", blur: 4, offset: 2, angle: 135, opacity: 0.1,
});

// ============================================================
// Visual Helpers
// ============================================================

/** Thin colored bar at bottom — visual anchor to balance whitespace */
function addAccentBar(slide) {
  slide.addShape("rect", {
    x: MARGIN_SIDE,
    y: SLIDE_H - 0.12,
    w: 1.0,
    h: 0.035,
    fill: { color: C.primary },
  });
}

/** Slide number in bottom-right corner */
function addSlideNumber(slide, num, total, isDark) {
  slide.addText(`${num} / ${total}`, {
    x: SLIDE_W - 1.2,
    y: SLIDE_H - 0.3,
    w: 0.8,
    h: 0.25,
    fontSize: 9,
    fontFace: BODY_FONT,
    color: isDark ? C.muted : "AAAAAA",
    align: "right",
  });
}

/**
 * Compute adaptive font size and paragraph spacing for bullet slides.
 *
 * When there are few bullets, increases font and spacing to fill
 * the available vertical space, eliminating the "top-heavy" look.
 */
function adaptiveBulletStyle(bullets, availableH, boxWidth) {
  const n = bullets.length;

  // Try escalating font sizes until content fills 50-85% of space
  const candidates = [20, 18, 16, 14];
  let bestFont = 16;
  let bestPara = 8;

  for (const fs of candidates) {
    const estH = estimateHeight(bullets, fs, boxWidth);
    const fillRatio = estH / availableH;

    if (fillRatio <= 0.85) {
      bestFont = fs;
      // Distribute remaining space as paragraph spacing
      const remaining = availableH - estH;
      // Convert remaining inches to points, distribute across bullets
      bestPara = n > 1
        ? Math.min(32, Math.max(8, Math.round((remaining / (n - 1)) * 72 * 0.5)))
        : 8;
      break;
    }
  }

  return { fontSize: bestFont, paraSpace: bestPara };
}

// ============================================================
// Slide Builders
// ============================================================

function addTitle(slide, s) {
  slide.addText(s.title || CONFIG.title || "Untitled", {
    x: 0.8, y: 1.0, w: 8.4, h: 1.6,
    fontSize: 28, fontFace: HEADER_FONT,
    color: C.accent, bold: true, align: "left",
    lineSpacingMultiple: 1.1,
  });
  const extra = s.extra || {};
  const authors = extra.authors || s.subtitle || CONFIG.authors || "";
  if (authors) {
    slide.addText(authors, {
      x: 0.8, y: 2.8, w: 8.4, h: 0.4,
      fontSize: 18, fontFace: BODY_FONT, color: C.secondary,
    });
  }
  const affil = extra.affiliation || CONFIG.affiliation || "";
  const venue = CONFIG.venue || "";
  const bottom = [affil, venue].filter(Boolean).join("  ·  ");
  if (bottom) {
    slide.addText(bottom, {
      x: 0.8, y: 3.3, w: 8.4, h: 0.35,
      fontSize: 14, fontFace: BODY_FONT, color: C.muted,
    });
  }
}

function addSection(slide, s) {
  slide.addText(s.title, {
    x: 0.8, y: 2.0, w: 8.4, h: 1.2,
    fontSize: 30, fontFace: HEADER_FONT,
    color: C.accent, bold: true, align: "left",
  });
}

function addContentSlide(slide, s) {
  let cursor = MARGIN_TOP;

  slide.addText(s.title, {
    x: MARGIN_SIDE, y: cursor, w: CONTENT_W, h: TITLE_H,
    fontSize: TITLE_FONT, fontFace: HEADER_FONT,
    color: C.primary, bold: true, margin: 0,
  });
  cursor += TITLE_H + GAP;

  const availableH = SLIDE_H - cursor - MARGIN_BOTTOM;

  if (s.bullets && s.bullets.length > 0) {
    const style = adaptiveBulletStyle(s.bullets, availableH, CONTENT_W);

    // Compute content height and vertically offset if sparse
    const contentH = estimateHeight(s.bullets, style.fontSize, CONTENT_W);
    const fillRatio = contentH / availableH;
    let bulletY = cursor;

    // If content is sparse (<45% fill), push down slightly for visual balance
    if (fillRatio < 0.35) {
      bulletY = cursor + (availableH - contentH) * 0.15;
    }

    slide.addText(
      s.bullets.map((b, i) => ({
        text: b,
        options: { bullet: true, breakLine: i < s.bullets.length - 1 },
      })),
      {
        x: MARGIN_SIDE, y: bulletY, w: CONTENT_W,
        h: availableH - (bulletY - cursor),
        fontSize: style.fontSize, fontFace: BODY_FONT,
        color: C.bodyText, paraSpaceAfter: style.paraSpace, valign: "top",
      }
    );
  }

  addAccentBar(slide);
}

async function addContentFigureSlide(slide, s) {
  let cursor = MARGIN_TOP;

  slide.addText(s.title, {
    x: MARGIN_SIDE, y: cursor, w: CONTENT_W, h: TITLE_H,
    fontSize: TITLE_FONT, fontFace: HEADER_FONT,
    color: C.primary, bold: true, margin: 0,
  });
  cursor += TITLE_H + GAP;

  const captionReserve = s.figure_caption && s.figure_caption.trim() ? 0.5 : 0.1;
  const availableH = SLIDE_H - cursor - MARGIN_BOTTOM - captionReserve;

  const hasFigure = s.figure_path && fs.existsSync(s.figure_path);
  let naturalDims = null;
  if (hasFigure) {
    naturalDims = await fitImage(s.figure_path, 100, 100);  // probe aspect ratio
  }

  // ── Decide layout mode based on aspect ratio ──
  // Wide-short figure (>2.2:1) OR tall-narrow (<1:2) → stack vertically
  let layoutMode = "side-by-side";
  if (naturalDims) {
    const aspect = naturalDims.w / naturalDims.h;
    if (aspect > 2.2 || aspect < 0.45) {
      layoutMode = "stacked";
    }
  }

  if (layoutMode === "stacked") {
    // Figure on top, bullets below (or vice versa for tall figures)
    const isVeryTall = naturalDims && naturalDims.w / naturalDims.h < 0.45;

    if (isVeryTall) {
      // Tall figure on the RIGHT, bullets fill left full-height
      const leftW = 5.5;
      const rightW = 3.5;
      const bulletStyle = s.bullets
        ? adaptiveBulletStyle(s.bullets, availableH, leftW)
        : { fontSize: 16, paraSpace: 8 };

      if (s.bullets && s.bullets.length > 0) {
        slide.addText(
          s.bullets.map((b, i) => ({
            text: b,
            options: { bullet: true, breakLine: i < s.bullets.length - 1 },
          })),
          {
            x: MARGIN_SIDE, y: cursor, w: leftW, h: availableH,
            fontSize: bulletStyle.fontSize, fontFace: BODY_FONT,
            color: C.bodyText, paraSpaceAfter: bulletStyle.paraSpace, valign: "top",
          }
        );
      }

      const fig = await fitImage(s.figure_path, rightW, availableH);
      const figX = SLIDE_W - MARGIN_SIDE - fig.w;
      const figY = cursor + (availableH - fig.h) / 2;
      slide.addImage({
        path: s.figure_path,
        x: figX, y: figY, w: fig.w, h: fig.h,
      });

      // Caption aligned under the tall figure
      if (s.figure_caption && s.figure_caption.trim().length > 0) {
        const captionY = Math.min(figY + fig.h + 0.08, SLIDE_H - MARGIN_BOTTOM - 0.3);
        const capPad = 0.1;
        const capX = Math.max(MARGIN_SIDE, figX - capPad);
        const capW = Math.min(fig.w + 2 * capPad, SLIDE_W - MARGIN_SIDE - capX);
        slide.addText(s.figure_caption, {
          x: capX, y: captionY,
          w: capW, h: 0.35,
          fontSize: 11, fontFace: BODY_FONT,
          color: C.muted, italic: true, align: "center",
        });
      }
    } else {
      // Wide figure: bullets on top, figure below full width
      const bulletH = Math.min(availableH * 0.35, 1.8);
      const figH = availableH - bulletH - 0.15;

      if (s.bullets && s.bullets.length > 0) {
        const bulletStyle = adaptiveBulletStyle(s.bullets, bulletH, CONTENT_W);
        slide.addText(
          s.bullets.map((b, i) => ({
            text: b,
            options: { bullet: true, breakLine: i < s.bullets.length - 1 },
          })),
          {
            x: MARGIN_SIDE, y: cursor, w: CONTENT_W, h: bulletH,
            fontSize: bulletStyle.fontSize, fontFace: BODY_FONT,
            color: C.bodyText, paraSpaceAfter: bulletStyle.paraSpace, valign: "top",
          }
        );
      }

      const fig = await fitImage(s.figure_path, CONTENT_W, figH);
      slide.addImage({
        path: s.figure_path,
        x: (SLIDE_W - fig.w) / 2,
        y: cursor + bulletH + 0.15 + (figH - fig.h) / 2,
        w: fig.w, h: fig.h,
      });

      // Wide figure occupies near-full width → page-centered caption is fine
      if (s.figure_caption && s.figure_caption.trim().length > 0) {
        slide.addText(s.figure_caption, {
          x: MARGIN_SIDE, y: SLIDE_H - MARGIN_BOTTOM - 0.35,
          w: CONTENT_W, h: 0.35,
          fontSize: 11, fontFace: BODY_FONT,
          color: C.muted, italic: true, align: "center",
        });
      }
    }

    addAccentBar(slide);
    return;
  }

  // ── Standard side-by-side layout ──
  let figDims = null;
  if (hasFigure) {
    figDims = await fitImage(s.figure_path, CONTENT_W * 0.5, availableH);
  }

  let leftW, rightW;
  if (figDims && figDims.w > figDims.h * 1.3) {
    leftW = 3.5;
    rightW = 5.0;
  } else if (figDims && figDims.h > figDims.w * 1.3) {
    leftW = 5.0;
    rightW = 3.5;
  } else {
    leftW = 4.3;
    rightW = 4.2;
  }

  if (hasFigure) {
    figDims = await fitImage(s.figure_path, rightW, availableH);
  }

  const figH = figDims ? figDims.h : 0;
  const bulletStyle = s.bullets
    ? adaptiveBulletStyle(s.bullets, availableH, leftW)
    : { fontSize: 16, paraSpace: 8 };
  const bulletH = s.bullets
    ? estimateHeight(s.bullets, bulletStyle.fontSize, leftW)
    : 0;

  const contentH = Math.max(bulletH, figH);
  const slack = availableH - contentH;
  const verticalOffset = (slack > 0.5 && contentH / availableH < 0.7)
    ? slack * 0.15
    : 0;
  const columnY = cursor + verticalOffset;

  if (s.bullets && s.bullets.length > 0) {
    slide.addText(
      s.bullets.map((b, i) => ({
        text: b,
        options: { bullet: true, breakLine: i < s.bullets.length - 1 },
      })),
      {
        x: MARGIN_SIDE, y: columnY, w: leftW, h: contentH,
        fontSize: bulletStyle.fontSize, fontFace: BODY_FONT,
        color: C.bodyText, paraSpaceAfter: bulletStyle.paraSpace, valign: "top",
      }
    );
  }

  if (figDims && hasFigure) {
    const figY = columnY + (contentH - figDims.h) / 2;
    slide.addImage({
      path: s.figure_path,
      x: SLIDE_W - MARGIN_SIDE - figDims.w,
      y: figY, w: figDims.w, h: figDims.h,
    });
  }

  if (s.figure_caption && s.figure_caption.trim().length > 0 && figDims) {
  const captionY = Math.min(
    columnY + contentH + 0.1,
    SLIDE_H - MARGIN_BOTTOM - 0.3,
  );
  const figX = SLIDE_W - MARGIN_SIDE - figDims.w;
  // Caption spans the figure's actual width (with a small horizontal padding)
  const capPad = 0.1;
  const capX = Math.max(MARGIN_SIDE, figX - capPad);
  const capW = Math.min(
    figDims.w + 2 * capPad,
    SLIDE_W - MARGIN_SIDE - capX,
  );
  slide.addText(s.figure_caption, {
    x: capX, y: captionY,
    w: capW, h: 0.35,
    fontSize: 11, fontFace: BODY_FONT,
    color: C.muted, italic: true, align: "center",
  });
}

  addAccentBar(slide);
}

async function addFullFigureSlide(slide, s) {
  let cursor = MARGIN_TOP;

  slide.addText(s.title, {
    x: MARGIN_SIDE, y: cursor, w: CONTENT_W, h: TITLE_H,
    fontSize: TITLE_FONT, fontFace: HEADER_FONT,
    color: C.primary, bold: true, margin: 0,
  });
  cursor += TITLE_H + GAP;

  const hasFigure = s.figure_path && fs.existsSync(s.figure_path);
  const maxFigH = SLIDE_H - cursor - MARGIN_BOTTOM - 0.5;

  if (hasFigure) {
    const d = await fitImage(s.figure_path, CONTENT_W, maxFigH);
    // Center figure both horizontally and vertically
    const cx = (SLIDE_W - d.w) / 2;
    const cy = cursor + (maxFigH - d.h) / 2;
    slide.addImage({ path: s.figure_path, x: cx, y: cy, w: d.w, h: d.h });
    cursor = cy + d.h + 0.1;
  }

  if (s.figure_caption && s.figure_caption.trim().length > 0) {
    slide.addText(s.figure_caption, {
      x: MARGIN_SIDE, y: Math.min(cursor, SLIDE_H - 0.4),
      w: CONTENT_W, h: 0.4,
      fontSize: 11, fontFace: BODY_FONT,
      color: C.muted, italic: true, align: "center",
    });
  }

  addAccentBar(slide);
}

function addMetricsSlide(slide, s) {
  let cursor = MARGIN_TOP;

  slide.addText(s.title, {
    x: MARGIN_SIDE, y: cursor, w: CONTENT_W, h: TITLE_H,
    fontSize: TITLE_FONT, fontFace: HEADER_FONT,
    color: C.primary, bold: true, margin: 0,
  });
  cursor += TITLE_H + GAP;

  const metrics = s.metrics || [];
  const n = metrics.length || 1;
  const cardGap = 0.4;
  const cardW = (CONTENT_W - cardGap * (n - 1)) / n;
  const cardH = 1.5;

  // Vertically center the metric cards in available space
  const availableH = SLIDE_H - cursor - MARGIN_BOTTOM;
  const vertOffset = (availableH - cardH) / 2;
  const cardY = cursor + Math.max(0, vertOffset * 0.6);

  // ── Adaptive value font: scale down for narrow cards or long text ──
  // At 36pt, ~cardW inches fits ~(cardW * 72 / (36 * 0.55)) chars of Latin text.
  // We want the value text to fit within 85% of the card width with some padding.
  const valuePad = 0.2;  // inches of horizontal padding inside the card
  const valueW = Math.max(cardW - 2 * valuePad, 1.0);

  const longestValue = metrics.reduce(
    (max, m) => Math.max(max, String(m.value || "").length), 0
  );
  const longestLabel = metrics.reduce(
    (max, m) => Math.max(max, String(m.label || "").length), 0
  );

  // Value font: start at 36pt, scale down if needed
  let valueFontSize = 36;
  {
    // Estimate: at this fontSize, how many chars fit in valueW inches?
    const maxChars = (valueW * 72) / (valueFontSize * 0.55);
    if (longestValue > maxChars * 0.9) {
      // Scale down so the longest value fits comfortably
      const needed = Math.floor((valueW * 72) / (longestValue / 0.85 * 0.55));
      valueFontSize = Math.max(18, needed);
    }
    // Also shrink for many small cards: 4+ cards → smaller font
    if (n >= 4 && valueFontSize > 26) {
      valueFontSize = Math.max(22, valueFontSize - 6);
    } else if (n === 3 && valueFontSize > 30) {
      valueFontSize = Math.max(24, valueFontSize - 4);
    }
  }

  // Label font: start at 12pt, scale down for narrow cards
  let labelFontSize = 12;
  {
    const maxChars = (valueW * 72) / (labelFontSize * 0.55);
    if (longestLabel > maxChars * 0.9) {
      const needed = Math.floor((valueW * 72) / (longestLabel / 0.85 * 0.55));
      labelFontSize = Math.max(8, needed);
    }
    if (n >= 4 && labelFontSize > 10) {
      labelFontSize = Math.max(8, labelFontSize - 2);
    }
  }

  metrics.forEach((m, i) => {
    const x = MARGIN_SIDE + i * (cardW + cardGap);
    slide.addShape("rect", {
      x, y: cardY, w: cardW, h: cardH,
      fill: { color: C.tableFill },
      shadow: makeShadow(),
    });
    slide.addText(m.value || "", {
      x: x + valuePad, y: cardY + 0.15, w: valueW, h: 0.8,
      fontSize: valueFontSize, fontFace: HEADER_FONT,
      color: C.primary, bold: true, align: "center", valign: "bottom", margin: 0,
    });
    slide.addText(m.label || "", {
      x: x + valuePad, y: cardY + 0.95, w: valueW, h: 0.4,
      fontSize: labelFontSize, fontFace: BODY_FONT,
      color: C.muted, align: "center", valign: "top", margin: 0,
    });
  });

  // Notes below cards
  if (s.notes) {
    slide.addText(s.notes, {
      x: MARGIN_SIDE, y: cardY + cardH + 0.2, w: CONTENT_W, h: 0.35,
      fontSize: 11, fontFace: BODY_FONT, color: C.muted, italic: true,
    });
  }

  addAccentBar(slide);
}

function addTableSlide(slide, s) {
  let cursor = MARGIN_TOP;

  slide.addText(s.title, {
    x: MARGIN_SIDE, y: cursor, w: CONTENT_W, h: TITLE_H,
    fontSize: TITLE_FONT, fontFace: HEADER_FONT,
    color: C.primary, bold: true, margin: 0,
  });
  cursor += TITLE_H + GAP;

  const headers = s.table_headers || [];
  const rows = s.table_rows || [];
  const numCols = headers.length || 1;
  const availableH = SLIDE_H - cursor - MARGIN_BOTTOM;

  // ── Adaptive table sizing ──
  // Scale font size and row heights so the table fits the available space.
  // Base: header 0.45", data rows 0.40", font 13pt (same as before).
  // When there are many rows, shrink proportionally (floor 9pt / 0.28").
  const baseHeaderH = 0.45;
  const baseRowH = 0.40;
  const baseFont = 13;
  const totalBaseH = baseHeaderH + rows.length * baseRowH;

  let fontSize, headerH, rowH;
  if (totalBaseH <= availableH) {
    fontSize = baseFont;
    headerH = baseHeaderH;
    rowH = rows.length > 0
      ? Math.min(baseRowH, (availableH - headerH) / rows.length)
      : baseRowH;
  } else {
    // Scale down proportionally, with a hard floor
    const scale = Math.max(0.55, availableH / totalBaseH);
    fontSize = Math.max(9, Math.floor(baseFont * scale));
    headerH = Math.max(0.28, baseHeaderH * scale);
    rowH = rows.length > 0
      ? Math.max(0.25, (availableH - headerH) / rows.length)
      : baseRowH * scale;
  }

  // ── Check long cell text and reduce font further if needed ──
  const colW = CONTENT_W / numCols;
  let minCellFont = fontSize;
  for (const row of rows) {
    for (const cell of row) {
      const text = String(cell || "");
      if (text.length > 20) {
        // Estimate: if the longest word in this cell doesn't fit at current
        // font size, reduce the font to keep it on one line.
        const words = text.split(/\s+/);
        let maxWordLen = 0;
        for (const w of words) { maxWordLen = Math.max(maxWordLen, w.length); }
        // Rough heuristic: at fontSize 13, ~colW inches fits ~(colW*72/(13*0.55)) chars
        const charsPerInch = 72 / (fontSize * 0.55);
        const cellChars = colW * charsPerInch;
        if (maxWordLen > cellChars * 0.85) {
          const neededFont = Math.floor(72 / (maxWordLen / (colW * 0.85) * 0.55));
          minCellFont = Math.min(minCellFont, Math.max(8, neededFont));
        }
      }
    }
  }
  if (minCellFont < fontSize) {
    fontSize = minCellFont;
    // Don't re-scale row heights — keep the same allocation.
  }

  // Slightly larger header font
  const hFontSize = Math.min(fontSize + 1, 14);

  const hOpts = {
    fill: { color: C.tableHead }, color: "FFFFFF",
    bold: true, fontSize: hFontSize, fontFace: BODY_FONT, align: "center",
    valign: "middle",
  };
  const cOpts = {
    fontSize: fontSize, fontFace: BODY_FONT, color: C.bodyText, align: "center",
    valign: "middle",
  };
  const hlOpts = { ...cOpts, bold: true, color: C.primary };

  const headerRow = headers.map(h => ({ text: h, options: hOpts }));
  const dataRows = rows.map((row, rowIdx) =>
    row.map(cell => ({
      text: String(cell),
      options: rowIdx === s.highlight_row ? hlOpts : cOpts,
    }))
  );

  const allRows = [headerRow, ...dataRows];
  const totalRowH = headerH + rows.length * rowH;

  // Vertically center table in available space
  const vertOffset = (availableH - totalRowH) > 0.3
    ? (availableH - totalRowH) * 0.3
    : 0;
  const tableY = cursor + vertOffset;

  slide.addTable(allRows, {
    x: MARGIN_SIDE, y: tableY, w: CONTENT_W,
    colW: Array(numCols).fill(colW),
    border: { pt: 0.5, color: "D1D5DB" },
    rowH: [headerH, ...Array(rows.length).fill(rowH)],
    autoPage: false,
  });

  addAccentBar(slide);
}

function addConclusionSlide(slide, s) {
  const availableH = SLIDE_H - MARGIN_TOP - MARGIN_BOTTOM;

  // Title
  const titleY = MARGIN_TOP + 0.2;
  slide.addText(s.title || "Key Takeaways", {
    x: 0.8, y: titleY, w: 8.4, h: 0.6,
    fontSize: 26, fontFace: HEADER_FONT,
    color: C.accent, bold: true, margin: 0,
  });
  const bulletStart = titleY + 0.6 + 0.3;
  const bulletAvail = SLIDE_H - bulletStart - 1.0;

  if (s.bullets && s.bullets.length > 0) {
    const style = adaptiveBulletStyle(s.bullets, bulletAvail, 8.4);
    // Use at least 20pt for conclusion takeaways
    const fs = Math.max(style.fontSize, 20);

    slide.addText(
      s.bullets.map((t, i) => ({
        text: t,
        options: { bullet: true, breakLine: i < s.bullets.length - 1 },
      })),
      {
        x: 0.8, y: bulletStart, w: 8.4, h: bulletAvail,
        fontSize: fs, fontFace: BODY_FONT,
        color: C.secondary, paraSpaceAfter: style.paraSpace, valign: "top",
      }
    );
  }

  slide.addText("Thank you · Questions?", {
    x: 0.8, y: SLIDE_H - 0.6, w: 8.4, h: 0.4,
    fontSize: 16, fontFace: BODY_FONT,
    color: C.muted, italic: true, align: "center",
  });
}

// ── NEW: Complex Layout Functions ──

function addTwoColumnSlide(slide, s) {
  let cursor = MARGIN_TOP;

  // Title
  slide.addText(s.title, {
    x: MARGIN_SIDE, y: cursor, w: CONTENT_W, h: TITLE_H,
    fontSize: TITLE_FONT, fontFace: HEADER_FONT,
    color: C.primary, bold: true, margin: 0,
  });
  cursor += TITLE_H + GAP;

  const columns = s.extra?.columns || [];
  const n = Math.min(columns.length, 3); // support 2 or 3 columns
  if (n === 0) { addAccentBar(slide); return; }

  const gutter = 0.25;
  const colW = (CONTENT_W - gutter * (n - 1)) / n;
  const availableH = SLIDE_H - cursor - MARGIN_BOTTOM - 0.3;

  columns.forEach((col, i) => {
    const x = MARGIN_SIDE + i * (colW + gutter);
    let cy = cursor;

    // Column sub-title (bold, accent color)
    if (col.sub_title) {
      slide.addText(col.sub_title, {
        x, y: cy, w: colW, h: 0.35,
        fontSize: 15, fontFace: HEADER_FONT,
        color: C.primary, bold: true, margin: 0,
      });
      cy += 0.35 + 0.12;
    }

    // Column bullets
    const bullets = col.bullets || [];
    if (bullets.length > 0) {
      const bulletAvailH = availableH - (cy - cursor) - 0.2;
      slide.addText(
        bullets.map((b, bi) => ({
          text: b,
          options: { bullet: true, breakLine: bi < bullets.length - 1 },
        })),
        {
          x, y: cy, w: colW, h: bulletAvailH,
          fontSize: 13, fontFace: BODY_FONT,
          color: C.bodyText, paraSpaceAfter: 6, valign: "top",
        }
      );
    }

    // Column figure (optional, small inline)
    if (col.figure_path) {
      // rendered via addImage — but we need async; skipped in sync flow for now
      // fallback: just show caption as italic note
      if (col.figure_caption) {
        slide.addText(col.figure_caption, {
          x, y: SLIDE_H - 0.5, w: colW, h: 0.3,
          fontSize: 10, fontFace: BODY_FONT,
          color: C.muted, italic: true,
        });
      }
    }
  });

  // Emphasis note below all columns
  if (s.extra?.emphasis_note) {
    slide.addText(s.extra.emphasis_note, {
      x: MARGIN_SIDE, y: SLIDE_H - MARGIN_BOTTOM - 0.35,
      w: CONTENT_W, h: 0.35,
      fontSize: 11, fontFace: BODY_FONT,
      color: C.muted, italic: true,
    });
  }

  addAccentBar(slide);
}

function addIconRowsSlide(slide, s, pres) {
  let cursor = MARGIN_TOP;

  slide.addText(s.title, {
    x: MARGIN_SIDE, y: cursor, w: CONTENT_W, h: TITLE_H,
    fontSize: TITLE_FONT, fontFace: HEADER_FONT,
    color: C.primary, bold: true, margin: 0,
  });
  cursor += TITLE_H + GAP;

  const rows = s.extra?.rows || [];
  const rowH = 1.05;
  const rowGap = 0.15;
  const availableH = SLIDE_H - cursor - MARGIN_BOTTOM;
  const totalH = rows.length * rowH + (rows.length - 1) * rowGap;
  const startY = cursor + Math.max(0, (availableH - totalH) / 2);

  rows.forEach((row, i) => {
    const y = startY + i * (rowH + rowGap);

    // Left label box (small square with number/emoji)
    slide.addShape("rect", {
      x: MARGIN_SIDE, y, w: 0.6, h: rowH,
      fill: { color: C.tableFill },
      shadow: makeShadow(),
    });
    slide.addText(row.label || `${i + 1}`, {
      x: MARGIN_SIDE, y, w: 0.6, h: rowH,
      fontSize: 18, fontFace: HEADER_FONT,
      color: C.primary, bold: true, align: "center", valign: "middle", margin: 0,
    });

    // Header + description
    const textX = MARGIN_SIDE + 0.6 + 0.2;
    const textW = CONTENT_W - 0.6 - 0.2;
    if (row.header) {
      slide.addText(row.header, {
        x: textX, y: y + 0.08, w: textW, h: 0.4,
        fontSize: 16, fontFace: HEADER_FONT,
        color: C.primary, bold: true, margin: 0,
      });
    }
    if (row.description) {
      slide.addText(row.description, {
        x: textX, y: y + 0.5, w: textW, h: 0.45,
        fontSize: 12, fontFace: BODY_FONT,
        color: C.bodyText, margin: 0,
      });
    }
  });

  addAccentBar(slide);
}

function addStepsSlide(slide, s, pres) {
  let cursor = MARGIN_TOP;

  slide.addText(s.title, {
    x: MARGIN_SIDE, y: cursor, w: CONTENT_W, h: TITLE_H,
    fontSize: TITLE_FONT, fontFace: HEADER_FONT,
    color: C.primary, bold: true, margin: 0,
  });
  cursor += TITLE_H + GAP;

  const steps = s.extra?.steps || [];
  const n = Math.max(steps.length, 1);
  const stepGap = 0.3;
  const stepW = (CONTENT_W - stepGap * (n - 1)) / n;
  const availableH = SLIDE_H - cursor - MARGIN_BOTTOM;
  const totalCardH = Math.min(2.6, availableH * 0.7);
  const cardY = cursor + (availableH - totalCardH) / 2;

  steps.forEach((step, i) => {
    const x = MARGIN_SIDE + i * (stepW + stepGap);

    // Card background
    slide.addShape("rect", {
      x, y: cardY, w: stepW, h: totalCardH,
      fill: { color: i === 0 ? C.primary : C.tableFill },
      shadow: makeShadow(),
    });

    const isFirstDark = i === 0;

    // Step number circle
    const circleSize = 0.5;
    slide.addShape("oval", {
      x: x + (stepW - circleSize) / 2, y: cardY + 0.2,
      w: circleSize, h: circleSize,
      fill: { color: isFirstDark ? C.secondary : C.primary },
    });
    slide.addText(step.step_number || `${i + 1}`, {
      x: x + (stepW - circleSize) / 2, y: cardY + 0.2,
      w: circleSize, h: circleSize,
      fontSize: 18, fontFace: HEADER_FONT,
      color: isFirstDark ? C.primary : "FFFFFF",
      bold: true, align: "center", valign: "middle", margin: 0,
    });

    // Step title
    if (step.step_title) {
      slide.addText(step.step_title, {
        x: x + 0.15, y: cardY + 0.85, w: stepW - 0.3, h: 0.45,
        fontSize: 14, fontFace: HEADER_FONT,
        color: isFirstDark ? "FFFFFF" : C.primary,
        bold: true, align: "center", valign: "middle", margin: 0,
      });
    }

    // Step description
    if (step.step_desc) {
      slide.addText(step.step_desc, {
        x: x + 0.15, y: cardY + 1.35, w: stepW - 0.3, h: 0.9,
        fontSize: 11, fontFace: BODY_FONT,
        color: isFirstDark ? C.secondary : C.bodyText,
        align: "center", valign: "top", margin: 0,
      });
    }

    // Arrow between steps (except last)
    if (i < n - 1) {
      const arrowX = x + stepW + stepGap * 0.3;
      const arrowY = cardY + totalCardH / 2;
      slide.addText("→", {
        x: arrowX - 0.15, y: arrowY - 0.2, w: stepGap * 0.4, h: 0.4,
        fontSize: 20, fontFace: BODY_FONT,
        color: C.muted, align: "center", valign: "middle", margin: 0,
      });
    }
  });

  addAccentBar(slide);
}

function addEmphasisBoxSlide(slide, s, pres) {
  let cursor = MARGIN_TOP;

  // Title
  slide.addText(s.title, {
    x: MARGIN_SIDE, y: cursor, w: CONTENT_W, h: TITLE_H,
    fontSize: TITLE_FONT, fontFace: HEADER_FONT,
    color: C.primary, bold: true, margin: 0,
  });
  cursor += TITLE_H + GAP;

  const boxReserve = (s.extra?.box_text) ? 1.0 : 0;
  const bulletAvailH = SLIDE_H - cursor - MARGIN_BOTTOM - boxReserve - 0.1;

  // Supporting bullets
  if (s.bullets && s.bullets.length > 0) {
    const style = adaptiveBulletStyle(s.bullets, bulletAvailH, CONTENT_W);
    slide.addText(
      s.bullets.map((b, i) => ({
        text: b,
        options: { bullet: true, breakLine: i < s.bullets.length - 1 },
      })),
      {
        x: MARGIN_SIDE, y: cursor, w: CONTENT_W, h: bulletAvailH,
        fontSize: style.fontSize, fontFace: BODY_FONT,
        color: C.bodyText, paraSpaceAfter: style.paraSpace, valign: "top",
      }
    );
  }

  // Emphasis box at bottom
  if (s.extra?.box_text) {
    const boxY = SLIDE_H - MARGIN_BOTTOM - boxReserve + 0.15;
    slide.addShape("rect", {
      x: MARGIN_SIDE - 0.1, y: boxY, w: CONTENT_W + 0.2, h: boxReserve - 0.25,
      fill: { color: C.tableFill },
      shadow: makeShadow(),
    });
    // Accent left bar
    slide.addShape("rect", {
      x: MARGIN_SIDE - 0.1, y: boxY, w: 0.06, h: boxReserve - 0.25,
      fill: { color: C.primary },
    });
    slide.addText(s.extra.box_text, {
      x: MARGIN_SIDE + 0.15, y: boxY + 0.1, w: CONTENT_W - 0.3, h: boxReserve - 0.45,
      fontSize: 13, fontFace: BODY_FONT,
      color: C.primary, bold: true, valign: "middle", margin: 0,
    });
  }

  addAccentBar(slide);
}

// ============================================================
// Main
// ============================================================
// ── Template master (from a user PPTX) ──
const TEMPLATE_MASTER = CONFIG.templateMaster || {};

function defineTemplateMasters(pres) {
  // Build one light + one dark master that carry the template's background,
  // logo, and (optionally) a title band. Slides reference them by name so the
  // user's PPTX branding is reused without copying its slide XML.
  const m = TEMPLATE_MASTER;
  const hasTemplate =
    m && (m.background || m.background_image || m.logo || m.title_band);
  if (!hasTemplate) return false;

  const objects = [];
  if (m.background_image && fs.existsSync(m.background_image)) {
    objects.push({ image: { path: m.background_image, x: 0, y: 0, w: SLIDE_W, h: SLIDE_H } });
  }
  if (m.title_band && m.title_band.color) {
    objects.push({
      rect: {
        x: 0, y: m.title_band.y ?? 0, w: SLIDE_W,
        h: m.title_band.h ?? 1.0, fill: { color: m.title_band.color },
      },
    });
  }
  if (m.logo && m.logo.path && fs.existsSync(m.logo.path)) {
    objects.push({
      image: {
        path: m.logo.path,
        x: m.logo.x ?? (SLIDE_W - 1.3), y: m.logo.y ?? 0.15,
        w: m.logo.w ?? 1.0, h: m.logo.h ?? 0.5,
      },
    });
  }

  pres.defineSlideMaster({
    title: "TPL_LIGHT",
    background: m.background_image
      ? { path: m.background_image }
      : { color: m.background || "FFFFFF" },
    objects: JSON.parse(JSON.stringify(objects)),
  });
  pres.defineSlideMaster({
    title: "TPL_DARK",
    background: { color: C.dark },
    objects: JSON.parse(JSON.stringify(objects)),
  });
  return true;
}

function addSlideFootnotes(slide, citations) {
  // Fit up to 3 footnote lines under the accent bar. When there are more
  // than 3 citations, we append "(+N more, see References)" so the audience
  // knows the full list is available on the References slide.
  const visible = citations.filter(c => (c.text || "").trim());
  const total = visible.length;
  const shown = visible.slice(0, 3);
  const lines = shown.map(c => `${c.key || ""} ${c.text || ""}`.trim());
  if (total > 3) {
    lines.push(`(+${total - 3} more, see References)`);
  }
  const baseY = SLIDE_H - 0.55;
  const lineH = 0.14;
  lines.forEach((txt, i) => {
    slide.addText(txt.slice(0, 130) + (txt.length > 130 ? "…" : ""), {
      x: MARGIN_SIDE, y: baseY + i * lineH,
      w: CONTENT_W - 1.3, h: lineH,
      fontSize: 9, fontFace: BODY_FONT,
      color: C.muted, italic: true,
    });
  });
}

function addReferencesSlide(slide, s) {
  slide.addText(s.title || "References", {
    x: MARGIN_SIDE, y: MARGIN_TOP, w: CONTENT_W, h: TITLE_H,
    fontSize: TITLE_FONT, fontFace: HEADER_FONT,
    color: C.primary, bold: true,
  });
  const items = s.bullets || [];
  const body = items.map((t, i) => ({
    text: t,
    options: { breakLine: i < items.length - 1 },
  }));
  slide.addText(body, {
    x: MARGIN_SIDE, y: MARGIN_TOP + TITLE_H + GAP,
    w: CONTENT_W, h: SLIDE_H - MARGIN_TOP - TITLE_H - GAP - MARGIN_BOTTOM,
    fontSize: 12, fontFace: BODY_FONT, color: C.bodyText,
    paraSpaceAfter: 6, valign: "top",
  });
  addAccentBar(slide);
}


async function main() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_16x9";
  pres.author = CONFIG.authors || "HelixForge";
  pres.title = CONFIG.title || "Research Presentation";
  const useTemplate = defineTemplateMasters(pres);
  const totalSlides = SLIDES.length;

  for (let idx = 0; idx < SLIDES.length; idx++) {
    const s = sanitizeSlide(SLIDES[idx]);
    const isDark = s.dark_background;
    // Reference the template master so the user's background/logo/title-band
    // is inherited automatically; fall back to a plain background otherwise.
    const slide = useTemplate
      ? pres.addSlide({ masterName: isDark ? "TPL_DARK" : "TPL_LIGHT" })
      : pres.addSlide();

    if (!useTemplate) {
      slide.background = isDark ? { color: C.dark } : { color: "FFFFFF" };
    }

    switch (s.slide_type) {
      case "title":
        addTitle(slide, s);
        break;
      case "section":
        addSection(slide, s);
        break;
      case "content":
        addContentSlide(slide, s);
        break;
      case "content_figure":
        await addContentFigureSlide(slide, s);
        break;
      case "full_figure":
        await addFullFigureSlide(slide, s);
        break;
      case "metrics":
        addMetricsSlide(slide, s);
        break;
      case "table":
        addTableSlide(slide, s);
        break;
      case "two_column":
        addTwoColumnSlide(slide, s);
        break;
      case "icon_rows":
        addIconRowsSlide(slide, s, pres);
        break;
      case "steps":
        addStepsSlide(slide, s, pres);
        break;
      case "emphasis_box":
        addEmphasisBoxSlide(slide, s, pres);
        break;
      case "conclusion":
        addConclusionSlide(slide, s);
        break;
      case "references":
        addReferencesSlide(slide, s);
        break;
      default:
        addContentSlide(slide, s);
    }

    if (s.slide_type !== "title" && s.slide_type !== "references" &&
        Array.isArray(s.citations) && s.citations.length > 0) {
      addSlideFootnotes(slide, s.citations);
    }

    // Slide number (skip title slide)
    if (s.slide_type !== "title") {
      addSlideNumber(slide, idx + 1, totalSlides, isDark);
    }
  }

  await pres.writeFile({ fileName: outputPath });
  console.log(`✅ Generated: ${outputPath} (${SLIDES.length} slides)`);
}

main().catch(err => {
    console.error(err);
    process.exit(1);
});
