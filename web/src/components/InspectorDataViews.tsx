import { useId, useMemo, useState, type ReactNode } from "react";
import { asRecord, prettyValue, shortId } from "../format";
import { IconChevronDown } from "../icons";
import { MarkdownBody } from "../markdown";

type ViewMode = "readable" | "raw";

type Metric = {
  label: string;
  value: string;
  hint?: string;
};

type Fact = {
  label: string;
  value: unknown;
};

const ROM_COUNT_LABELS: Array<[string, string]> = [
  ["sources", "来源"],
  ["chunks", "片段"],
  ["citations", "引用"],
  ["hypotheses", "假设"],
  ["claims", "主张"],
  ["evidence", "证据"],
  ["runs", "运行"],
];

function records(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => Boolean(item));
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function finiteNumber(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

function formatInteger(value: unknown): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(
    finiteNumber(value),
  );
}

function formatUsd(value: unknown): string {
  const amount = finiteNumber(value);
  if (amount === 0) return "$0.00";
  const digits = Math.abs(amount) < 1 ? 6 : 4;
  return `$${amount.toFixed(digits).replace(/0+$/, "").replace(/\.$/, "")}`;
}

function formatConfidence(value: unknown): string {
  const confidence = finiteNumber(value);
  if (!present(value)) return "—";
  return confidence <= 1 ? `${Math.round(confidence * 100)}%` : `${confidence}%`;
}

function present(value: unknown): boolean {
  if (value == null || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  return true;
}

function rawSource(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2) ?? "";
  } catch {
    return prettyValue(value);
  }
}

function scopeLabel(scope: unknown): string {
  if (scope === "session") return "当前会话";
  if (scope === "task") return "当前任务";
  return "当前工作区";
}

function ViewFrame({
  label,
  rawLabel,
  value,
  children,
}: {
  label: string;
  rawLabel: string;
  value: unknown;
  children: ReactNode;
}) {
  const [mode, setMode] = useState<ViewMode>("readable");
  const modeLabel = label === "ROM" ? "ROM 展示方式" : `${label}展示方式`;
  return (
    <div className="inspector-data-view">
      <div className="inspector-view-toolbar">
        <div className="inspector-view-toggle" role="group" aria-label={modeLabel}>
          <button
            type="button"
            aria-pressed={mode === "readable"}
            onClick={() => setMode("readable")}
          >
            可读视图
          </button>
          <button type="button" aria-pressed={mode === "raw"} onClick={() => setMode("raw")}>
            {rawLabel}
          </button>
        </div>
      </div>
      {mode === "raw" ? (
        <pre className="inspector-raw" tabIndex={0} aria-label={`${label}${rawLabel}`}>
          {rawSource(value)}
        </pre>
      ) : (
        children
      )}
    </div>
  );
}

function Overview({
  title,
  scope,
  description,
  metrics,
}: {
  title: string;
  scope: string;
  description: string;
  metrics: Metric[];
}) {
  return (
    <section className="inspector-overview" aria-label={title}>
      <header>
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
        <span>{scope}</span>
      </header>
      <dl className="inspector-metrics">
        {metrics.map((metric) => (
          <div key={metric.label}>
            <dt>{metric.label}</dt>
            <dd>{metric.value}</dd>
            {metric.hint ? <small>{metric.hint}</small> : null}
          </div>
        ))}
      </dl>
    </section>
  );
}

function Facts({ facts }: { facts: Fact[] }) {
  const visible = facts.filter((fact) => present(fact.value));
  if (!visible.length) return <div className="empty compact">暂无更多信息</div>;
  return (
    <dl className="inspector-facts">
      {visible.map((fact) => (
        <div key={fact.label}>
          <dt>{fact.label}</dt>
          <dd>{prettyValue(fact.value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function DisclosureSection({
  title,
  count,
  countLabel,
  description,
  defaultOpen,
  children,
}: {
  title: string;
  count?: number;
  countLabel?: string;
  description?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(Boolean(defaultOpen));
  const contentId = useId();
  const visibleCount = countLabel || (typeof count === "number" ? `${count} 个` : "");
  return (
    <section className={`inspector-data-section${open ? " expanded" : ""}`}>
      <button
        type="button"
        className="inspector-section-head"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={() => setOpen((value) => !value)}
      >
        <span>
          <strong>{title}</strong>
          {description ? <small>{description}</small> : null}
        </span>
        {visibleCount ? <b>{visibleCount}</b> : null}
        <IconChevronDown size={15} aria-hidden="true" />
      </button>
      {open ? (
        <div id={contentId} className="inspector-section-content">
          {children}
        </div>
      ) : null}
    </section>
  );
}

function RecordDisclosure({
  title,
  meta,
  facts,
}: {
  title: string;
  meta?: ReactNode;
  facts: Fact[];
}) {
  const [open, setOpen] = useState(false);
  const contentId = useId();
  const triggerId = useId();
  return (
    <div className={`inspector-record${open ? " expanded" : ""}`}>
      <button
        id={triggerId}
        type="button"
        className="inspector-record-head"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={() => setOpen((value) => !value)}
      >
        <span>
          <strong>{title}</strong>
          {meta ? <small>{meta}</small> : null}
        </span>
        <IconChevronDown size={14} aria-hidden="true" />
      </button>
      {open ? (
        <div
          id={contentId}
          className="inspector-record-detail"
          role="region"
          aria-labelledby={triggerId}
        >
          <Facts facts={facts} />
        </div>
      ) : null}
    </div>
  );
}

function unknownFacts(
  value: Record<string, unknown>,
  known: ReadonlySet<string>,
): Fact[] {
  return Object.entries(value)
    .filter(([key, item]) => !known.has(key) && present(item))
    .map(([key, item]) => ({ label: key, value: item }));
}

export function RomView({ value }: { value: unknown }) {
  const rom = asRecord(value);
  const counts = asRecord(rom?.counts) || {};
  const hypotheses = records(rom?.hypotheses);
  const claims = records(rom?.claims);
  const sources = records(rom?.sources);
  const runs = records(rom?.runs);
  const groups = [hypotheses, claims, sources, runs];
  const totalRows = groups.reduce((sum, rows) => sum + rows.length, 0);
  const firstNonEmpty = groups.findIndex((rows) => rows.length > 0);
  const metrics = ROM_COUNT_LABELS.map(([key, label]) => ({
    label,
    value: formatInteger(counts[key]),
  }));
  const extra = rom
    ? unknownFacts(
        rom,
        new Set([
          "scope",
          "session_id",
          "counts",
          "hypotheses",
          "claims",
          "sources",
          "runs",
        ]),
      )
    : [];
  const defaultOpen = (index: number) =>
    totalRows <= 12 || (index === firstNonEmpty && groups[index].length <= 8);
  const countLabel = (key: string, shown: number) => {
    const total = finiteNumber(counts[key]);
    return total > shown ? `显示 ${shown} / 共 ${formatInteger(total)}` : `${shown} 个`;
  };

  return (
    <ViewFrame label="ROM" rawLabel="原始 JSON" value={value}>
      {!rom ? (
        <div className="empty">暂无研究记忆</div>
      ) : (
        <>
          <Overview
            title="研究记忆概览"
            scope={scopeLabel(rom.scope)}
            description="来源、主张与证据的结构化研究记录"
            metrics={metrics}
          />
          <div className="inspector-section-list">
            {hypotheses.length ? (
              <DisclosureSection
                title="研究假设"
                countLabel={countLabel("hypotheses", hypotheses.length)}
                description="待验证的问题与当前置信度"
                defaultOpen={defaultOpen(0)}
              >
                {hypotheses.map((row, index) => (
                  <RecordDisclosure
                    key={stringValue(row.id) || `hypothesis-${index}`}
                    title={stringValue(row.statement) || `假设 ${index + 1}`}
                    meta={
                      <>
                        {stringValue(row.status) || "未标注"} · 置信度 {formatConfidence(row.confidence)}
                      </>
                    }
                    facts={[
                      { label: "ID", value: row.id },
                      { label: "状态", value: row.status },
                      { label: "置信度", value: formatConfidence(row.confidence) },
                      { label: "更新时间", value: row.updated_at },
                    ]}
                  />
                ))}
              </DisclosureSection>
            ) : null}
            {claims.length ? (
              <DisclosureSection
                title="研究主张"
                countLabel={countLabel("claims", claims.length)}
                description="可追踪、可核验的研究陈述"
                defaultOpen={defaultOpen(1)}
              >
                {claims.map((row, index) => (
                  <RecordDisclosure
                    key={stringValue(row.id) || `claim-${index}`}
                    title={stringValue(row.text) || `主张 ${index + 1}`}
                    meta={
                      <>
                        {stringValue(row.polarity) || "未标注"} · 置信度 {formatConfidence(row.confidence)}
                      </>
                    }
                    facts={[
                      { label: "ID", value: row.id },
                      { label: "极性", value: row.polarity },
                      { label: "置信度", value: formatConfidence(row.confidence) },
                      { label: "关联假设", value: row.hypothesis_id },
                    ]}
                  />
                ))}
              </DisclosureSection>
            ) : null}
            {sources.length ? (
              <DisclosureSection
                title="文献来源"
                countLabel={countLabel("sources", sources.length)}
                description="已纳入研究记忆的论文与资料"
                defaultOpen={defaultOpen(2)}
              >
                {sources.map((row, index) => (
                  <RecordDisclosure
                    key={stringValue(row.id) || `source-${index}`}
                    title={stringValue(row.title) || `来源 ${index + 1}`}
                    meta={[row.year, row.venue].filter(present).join(" · ") || "来源记录"}
                    facts={[
                      { label: "ID", value: row.id },
                      { label: "arXiv", value: row.arxiv_id },
                      { label: "DOI", value: row.doi },
                      { label: "年份", value: row.year },
                      { label: "发表 venue", value: row.venue },
                    ]}
                  />
                ))}
              </DisclosureSection>
            ) : null}
            {runs.length ? (
              <DisclosureSection
                title="研究运行"
                countLabel={countLabel("runs", runs.length)}
                description="已记录的实验与研究过程"
                defaultOpen={defaultOpen(3)}
              >
                {runs.map((row, index) => (
                  <RecordDisclosure
                    key={stringValue(row.id) || `run-${index}`}
                    title={stringValue(row.title) || `运行 ${index + 1}`}
                    meta={stringValue(row.status) || "未标注"}
                    facts={[
                      { label: "ID", value: row.id },
                      { label: "状态", value: row.status },
                    ]}
                  />
                ))}
              </DisclosureSection>
            ) : null}
            {extra.length ? (
              <DisclosureSection title="其他字段" count={extra.length} description="兼容扩展数据">
                <Facts facts={extra} />
              </DisclosureSection>
            ) : null}
            {!totalRows && !extra.length ? <div className="empty">暂无研究记录</div> : null}
          </div>
        </>
      )}
    </ViewFrame>
  );
}

function costMetrics(value: Record<string, unknown>): Metric[] {
  return [
    { label: "总费用", value: formatUsd(value.cost_usd) },
    { label: "总 Tokens", value: formatInteger(value.total_tokens) },
    { label: "输入 Tokens", value: formatInteger(value.prompt_tokens) },
    { label: "输出 Tokens", value: formatInteger(value.completion_tokens) },
    { label: "模型调用", value: formatInteger(value.calls) },
    {
      label: "估算调用",
      value: formatInteger(value.estimated_calls),
      hint: finiteNumber(value.estimated_calls) ? "费用含估算值" : undefined,
    },
  ];
}

function ComponentCostTable({ value }: { value: unknown }) {
  const components = asRecord(value);
  const rows = components ? Object.entries(components) : [];
  if (!rows.length) return <div className="empty compact">暂无组件费用明细</div>;
  return (
    <div className="inspector-table-wrap" role="region" aria-label="组件费用明细" tabIndex={0}>
      <table className="inspector-data-table">
        <thead>
          <tr>
            <th scope="col">组件</th>
            <th scope="col">调用</th>
            <th scope="col" className="optional-token-column">输入</th>
            <th scope="col" className="optional-token-column">输出</th>
            <th scope="col">Tokens</th>
            <th scope="col">费用</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, item]) => {
            const row = asRecord(item) || {};
            return (
              <tr key={name}>
                <th scope="row">{name}</th>
                <td>{formatInteger(row.calls)}</td>
                <td className="optional-token-column">{formatInteger(row.prompt_tokens)}</td>
                <td className="optional-token-column">{formatInteger(row.completion_tokens)}</td>
                <td>{formatInteger(row.total_tokens)}</td>
                <td>{formatUsd(row.cost_usd)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CostTask({
  row,
  defaultOpen,
}: {
  row: Record<string, unknown>;
  defaultOpen: boolean;
}) {
  const taskId = stringValue(row.task_id);
  return (
    <DisclosureSection
      title={taskId ? `Task ${shortId(taskId)}` : "Task 费用"}
      description={`${formatInteger(row.total_tokens)} tokens · ${formatInteger(row.calls)} 次调用`}
      defaultOpen={defaultOpen}
    >
      <div className="cost-task-summary">
        <Facts
          facts={[
            { label: "完整 Task ID", value: taskId },
            {
              label: "包含子任务",
              value: Math.max(0, recordsFromStrings(row.task_ids).length - 1),
            },
            { label: "费用", value: formatUsd(row.cost_usd) },
            { label: "总 Tokens", value: formatInteger(row.total_tokens) },
            { label: "估算调用", value: formatInteger(row.estimated_calls) },
          ]}
        />
        <ComponentCostTable value={row.components} />
      </div>
    </DisclosureSection>
  );
}

function recordsFromStrings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && Boolean(item))
    : [];
}

export function CostView({ value }: { value: unknown }) {
  const cost = asRecord(value);
  const tasks = records(cost?.tasks);
  const extra = cost
    ? unknownFacts(
        cost,
        new Set([
          "scope",
          "session_id",
          "task_id",
          "prompt_tokens",
          "completion_tokens",
          "total_tokens",
          "cost_usd",
          "calls",
          "estimated_calls",
          "task_ids",
          "components",
          "tasks",
        ]),
      )
    : [];
  return (
    <ViewFrame label="费用" rawLabel="原始 JSON" value={value}>
      {!cost ? (
        <div className="empty">暂无费用数据</div>
      ) : (
        <>
          <Overview
            title="费用概览"
            scope={scopeLabel(cost.scope)}
            description="模型调用、Token 使用与估算费用"
            metrics={costMetrics(cost)}
          />
          <div className="inspector-section-list">
            {tasks.map((row, index) => (
              <CostTask
                key={stringValue(row.task_id) || `cost-task-${index}`}
                row={row}
                defaultOpen={index === 0}
              />
            ))}
            {!tasks.length && present(cost.components) ? (
              <DisclosureSection title="组件明细" defaultOpen>
                <ComponentCostTable value={cost.components} />
              </DisclosureSection>
            ) : null}
            {extra.length ? (
              <DisclosureSection title="其他字段" count={extra.length} description="兼容扩展数据">
                <Facts facts={extra} />
              </DisclosureSection>
            ) : null}
          </div>
        </>
      )}
    </ViewFrame>
  );
}

export function NotebookView({ value }: { value: string }) {
  const sectionCount = useMemo(() => (value.match(/^##\s+/gm) || []).length, [value]);
  return (
    <ViewFrame label="笔记" rawLabel="原始内容" value={value}>
      {!value.trim() ? (
        <div className="empty">空笔记</div>
      ) : (
        <>
          <Overview
            title="研究笔记"
            scope="Markdown"
            description="按当前检查器范围筛选的实验记录与研究结论"
            metrics={[
              { label: "章节", value: formatInteger(sectionCount) },
              { label: "字符", value: formatInteger(value.length) },
            ]}
          />
          <article className="notebook-reading">
            <MarkdownBody source={value} />
          </article>
        </>
      )}
    </ViewFrame>
  );
}
