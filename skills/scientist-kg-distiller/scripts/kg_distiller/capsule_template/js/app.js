(function () {
  const data = window.SOUL_CAPSULE;
  if (!data || !data.overview || !data.tone || !data.patterns) {
    document.body.textContent = "人格图谱数据不完整。";
    return;
  }

  const overview = data.overview;
  const identity = overview.identity;
  const meta = overview.meta;
  const cores = overview.soul_core;
  const tone = data.tone;
  const patterns = data.patternIndex.map((item) => data.patterns[item.category]);
  const coreById = Object.fromEntries(cores.map((item) => [item.id, item]));
  const patternById = Object.fromEntries(patterns.map((item) => [item.id, item]));
  const detail = document.getElementById("detail-panel");

  const translations = {
    "artificial intelligence researcher": "人工智能研究者",
    "university teacher": "高校教师",
    "computer vision": "计算机视觉",
    "self-supervised learning": "自监督学习",
    "Tsinghua University": "清华大学",
    "The Chinese University of Hong Kong": "香港中文大学",
    "Microsoft Research Asia": "微软亚洲研究院",
    "Facebook": "Meta 人工智能研究院",
    "Massachusetts Institute of Technology": "麻省理工学院",
    "bachelor's degree": "学士",
    "Doctor of Philosophy": "博士",
    "stop-gradient": "停止梯度",
    "BN": "批归一化"
  };
  const contextLabels = {
    compute_constraint: "计算资源受限",
    time_pressure: "时间压力",
    scope_tradeoff: "研究范围取舍",
    exploration_phase: "早期探索",
    validation_budget: "验证预算受限"
  };

  const zh = (value) => translations[String(value)] || String(value || "");
  const readable = (value) => {
    let output = String(value || "");
    Object.entries(translations)
      .sort((a, b) => b[0].length - a[0].length)
      .forEach(([source, target]) => {
        output = output.split(source).join(target);
      });
    return output
      .replace(/\s+([，。；：])/g, "$1")
      .replace(/([，。；：])\s+/g, "$1")
      .replace(/'{1,2}([^']+)'{1,2}/g, "“$1”");
  };
  const escape = (value) => String(value || "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
  const text = (id, value) => {
    document.getElementById(id).textContent = value || "";
  };

  renderIdentity();

  const graph = window.createPersonalityGraph(
    document.getElementById("knowledge-graph"),
    {
      cores,
      tone,
      patterns,
      relations: data.relations
    },
    showSelection
  );

  const defaultCore = cores.find((item) => item.question === "P03") || cores[0];
  graph.select(defaultCore.id);
  showCore(defaultCore);

  function renderIdentity() {
    text("scientist-name", meta.scientist_name);
    text("graph-name", meta.scientist_name);

    const currentEmployment = [...(identity.employment_history || [])]
      .sort((a, b) => (b.start_year || 0) - (a.start_year || 0))[0];
    text("identity-line", [
      ...(identity.occupations || []).map(zh),
      ...(currentEmployment ? [zh(currentEmployment.organization)] : [])
    ].join(" · "));

    const portrait = document.getElementById("portrait");
    const fallback = document.getElementById("portrait-fallback");
    fallback.textContent = meta.scientist_name
      .split(/\s+/)
      .map((part) => part[0])
      .join("");
    if (overview.portrait.status === "available") {
      portrait.src = overview.portrait.local_path;
      portrait.alt = `${meta.scientist_name} 肖像`;
      portrait.hidden = false;
      fallback.hidden = true;
      const credit = document.getElementById("portrait-credit");
      credit.href = overview.portrait.source_page;
      credit.hidden = false;
    }

    const education = (identity.education_history || [])
      .map((item) => `${zh(item.institution)} · ${zh(item.degree || "学习")}`)
      .join("；");
    const employment = (identity.employment_history || [])
      .slice()
      .sort((a, b) => (a.start_year || 0) - (b.start_year || 0))
      .map((item) => {
        const period = item.start_year
          ? `${item.start_year}—${item.end_year || "至今"}`
          : "时间未记录";
        return `${zh(item.organization)}（${period}）`;
      })
      .join("；");
    const facts = [
      ["研究", (identity.research_fields || []).map(zh).join("、")],
      ["学习", education],
      ["任职", employment]
    ].filter((item) => item[1]);
    document.getElementById("identity-facts").innerHTML = facts.map(([label, value]) =>
      `<dl class="identity-fact"><dt>${escape(label)}</dt><dd>${escape(value)}</dd></dl>`
    ).join("");

    const metrics = [
      [meta.total_L3, "人格核心"],
      [meta.total_L2, "思维模式"],
      [meta.total_L1, "原始证据"]
    ];
    document.getElementById("metrics").innerHTML = metrics.map(([value, label]) =>
      `<div class="metric"><strong>${escape(value)}</strong><span>${escape(label)}</span></div>`
    ).join("");
  }

  function showSelection(selection) {
    if (selection.kind === "core") showCore(selection.data);
    if (selection.kind === "tone") showTone(selection.data);
    if (selection.kind === "pattern") showPattern(selection.data);
    if (selection.kind === "evidence") showEvidence(selection.data, selection.parent);
  }

  function detailHeader(kind, label, symbol) {
    return `<header class="detail-header">
      <div class="detail-symbol" aria-hidden="true">${escape(symbol)}</div>
      <div><p class="detail-kind">${escape(kind)}</p><h3>${escape(label)}</h3></div>
    </header>`;
  }

  function coreSummary(core) {
    const full = readable(core.stance);
    if (core.question !== "P03") return full;
    const marker = full.indexOf("Kaiming He 是一位");
    return marker >= 0 ? full.slice(marker).trim() : full;
  }

  function showCore(core) {
    const symbols = { P01: "价值", P02: "信念", P03: "认知" };
    const extra = core.question === "P01"
      ? renderValues()
      : core.question === "P03"
        ? renderTimeline()
        : "";
    detail.innerHTML = `
      ${detailHeader("人格核心", core.label, symbols[core.question] || "核心")}
      <section class="summary-card">
        <p class="summary-label">核心摘要</p>
        <p class="detail-lead">${escape(coreSummary(core))}</p>
      </section>
      <details class="detail-disclosure">
        <summary>推断依据</summary>
        <div class="disclosure-content">
          <p class="detail-copy">${escape(readable(core.explanation))}</p>
        </div>
      </details>
      ${extra}
      ${renderEvidenceDisclosure(core.evidence || [], true)}`;
  }

  function showTone(toneNode) {
    const exemplars = toneNode.tone_exemplars || [];
    detail.innerHTML = `
      ${detailHeader(`人格核心 · ${exemplars.length} 条原句`, toneNode.label, "语气")}
      <section class="summary-card tone-note">
        <p class="summary-label">原声样例</p>
        <p class="detail-copy">以下内容逐字来自论文引言或演讲转录；这里不概括立场，也不连接思维模式。</p>
      </section>
      <ol class="tone-exemplar-list">${exemplars.map((item, index) =>
        `<li><span>${escape(String(index + 1).padStart(2, "0"))}</span>
          <blockquote>${escape(item)}</blockquote></li>`
      ).join("")}</ol>`;
  }

  function renderValues() {
    return `<details class="detail-disclosure">
      <summary>五项科学价值</summary>
      <div class="disclosure-content value-cloud">${data.values.map((item) =>
        `<article class="value-item"><strong>${escape(item.name)}</strong>
          <p>${escape(readable(item.relative_priority))}</p>
          <p>${escape(readable(item.explanation))}</p></article>`
      ).join("")}</div>
    </details>`;
  }

  function renderTimeline() {
    const rows = [];
    (identity.education_history || []).forEach((item) => rows.push({
      start: item.start_year,
      end: item.end_year,
      place: zh(item.institution),
      role: zh(item.degree || "学习")
    }));
    (identity.employment_history || []).forEach((item) => rows.push({
      start: item.start_year,
      end: item.end_year,
      place: zh(item.organization),
      role: "任职"
    }));
    rows.sort((a, b) => (a.start || 0) - (b.start || 0));
    return `<details class="detail-disclosure">
      <summary>学习与任职轨迹</summary>
      <div class="disclosure-content">
        <ol class="timeline">${rows.map((item) => {
          const period = item.start
            ? `${item.start}${item.end ? `—${item.end}` : "—至今"}`
            : "时间未记录";
          return `<li><div class="period">${escape(period)}</div>
            <div class="place">${escape(item.place)}</div>
            <div class="role">${escape(item.role)}</div></li>`;
        }).join("")}</ol>
      </div>
    </details>`;
  }

  function renderEvidenceDisclosure(evidence, opened) {
    if (!evidence.length) return "";
    return `<details class="detail-disclosure"${opened ? " open" : ""}>
      <summary>原始证据 · ${escape(evidence.length)} 条</summary>
      <div class="disclosure-content">
        <ul class="evidence-list">${evidence.map((item) =>
          `<li class="evidence-card">
            <p class="evidence-paper">${escape(item.paper)}</p>
            <p class="evidence-observation">${escape(readable(item.observation))}</p>
          </li>`
        ).join("")}</ul>
      </div>
    </details>`;
  }

  function showPattern(pattern) {
    const related = relatedRelations(pattern.id);
    detail.innerHTML = `
      ${detailHeader(`思维模式 · ${pattern.evidence_count} 条证据支撑`, pattern.label, "模式")}
      <section class="summary-card">
        <p class="summary-label">模式说明</p>
        <p class="detail-lead">${escape(readable(pattern.description))}</p>
      </section>
      ${related}
      ${renderEvidenceDisclosure(pattern.evidence || [], false)}
      <button id="expand-evidence" class="action-button" type="button">在图中展开代表证据</button>`;
    document.getElementById("expand-evidence").addEventListener("click", () => {
      graph.showEvidence(pattern);
      document.getElementById("expand-evidence").textContent = "证据节点已展开";
    });
  }

  function relatedRelations(id) {
    const rows = [];
    (data.relations.enables || []).forEach((edge) => {
      if (edge.from === id || edge.to === id) {
        rows.push({
          label: edge.from === id ? "作为前提，帮助形成" : "建立在这一模式之上",
          other: labelFor(edge.from === id ? edge.to : edge.from),
          reason: edge.reason
        });
      }
    });
    (data.relations.reinforces || []).forEach((edge) => {
      if (edge.from === id || edge.to === id) {
        rows.push({
          label: "相互强化",
          other: labelFor(edge.from === id ? edge.to : edge.from),
          reason: edge.reason
        });
      }
    });
    (data.relations.tension || []).forEach((edge) => {
      if ((edge.between || []).includes(id)) {
        const otherId = edge.between.find((value) => value !== id);
        rows.push({
          label: `存在张力（${contextLabels[edge.context] || "特定约束"}）`,
          other: labelFor(otherId),
          reason: edge.reason
        });
      }
    });
    if (!rows.length) return "";
    return `<details class="detail-disclosure">
      <summary>图谱关系 · ${escape(rows.length)} 条</summary>
      <div class="disclosure-content">
        <ul class="relation-list">${rows.map((row) =>
          `<li><strong>${escape(row.label)}：${escape(row.other)}</strong>
            <div class="detail-meta">${escape(readable(row.reason))}</div></li>`
        ).join("")}</ul>
      </div>
    </details>`;
  }

  function labelFor(id) {
    return patternById[id]?.label || coreById[id]?.label || "相关节点";
  }

  function showEvidence(item, parent) {
    detail.innerHTML = `
      ${detailHeader(`原始论文证据 · ${parent.label}`, item.paper, "证据")}
      <p class="detail-meta">${escape(item.year || "年份未知")} · ${escape(item.section || "章节未记录")}</p>
      <section class="summary-card">
        <p class="summary-label">证据说明</p>
        <p class="detail-lead">${escape(readable(item.observation))}</p>
        <blockquote class="evidence-quote">${escape(item.excerpt)}</blockquote>
      </section>
      <button id="back-pattern" class="action-button" type="button">返回思维模式</button>`;
    document.getElementById("back-pattern").addEventListener("click", () => {
      graph.clearEvidence();
      graph.select(parent.id);
      showPattern(parent);
    });
  }
})();
