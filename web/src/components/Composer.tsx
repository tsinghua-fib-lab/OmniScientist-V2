import { useEffect, useRef } from "react";
import {
  IconApprove,
  IconClose,
  IconFolder,
  IconPaperclip,
  IconSend,
  IconSteer,
  IconStop,
} from "../icons";
import { actions, useAppState } from "../store";
import type { Mode } from "../types";

const MODES: { id: Mode; label: string }[] = [
  { id: "auto", label: "自动" },
  { id: "plan", label: "计划" },
  { id: "review", label: "审阅" },
];

export function Composer({
  blocked = false,
  blockedReason = "",
}: {
  blocked?: boolean;
  blockedReason?: string;
}) {
  const { workspace, composer, mode, streaming, attachments, sessionId, tasks } = useAppState();
  const area = useRef<HTMLTextAreaElement>(null);
  const awaitingTask = tasks.find(
    (task) => task.status === "awaiting_approval" && task.session_id === sessionId,
  );

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [composer]);

  if (!workspace) return null;
  const interactionBlocked = streaming || blocked;
  const canSend = workspace.writable && !interactionBlocked && Boolean(composer.trim());
  const disabledReason = !workspace.writable
    ? "此工作区只读，请先在 CLI 中信任该目录。"
    : blocked
      ? blockedReason
      : "";

  return (
    <div className="composer-wrap" data-testid="composer">
      <div className="composer" aria-busy={interactionBlocked}>
        {attachments.length > 0 && (
          <div className="chips" aria-label="已附加文件">
            {attachments.map((a) => (
              <button
                key={a.uri}
                type="button"
                className="chip"
                aria-label={`移除附件 ${a.name}`}
                onClick={() => actions.removeAttachment(a.uri)}
              >
                <IconPaperclip size={13} />
                <span>{a.name}</span>
                <IconClose size={13} />
              </button>
            ))}
          </div>
        )}
        <textarea
          ref={area}
          rows={1}
          placeholder={
            !workspace.writable
              ? "此目录只读。先在 CLI 运行 omni trust。"
              : blocked
                ? blockedReason
                : "询问、斜杠命令，或继续这条会话…"
          }
          aria-label="给 Omni 发送消息"
          aria-describedby="composer-hint"
          value={composer}
          disabled={!workspace.writable || interactionBlocked}
          onChange={(e) => actions.setComposer(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void actions.send();
            }
          }}
        />
        <div className="composer-bar">
          <label className="composer-tool attach" title="添加附件" aria-label="添加附件">
            <IconPaperclip size={17} />
            <input
              type="file"
              hidden
              multiple
              disabled={!workspace.writable || interactionBlocked}
              onChange={(e) => {
                if (e.target.files) void actions.attach(e.target.files);
                e.target.value = "";
              }}
            />
          </label>
          <div className="modes" role="group" aria-label="交互模式">
            {MODES.map((m) => (
              <button
                key={m.id}
                type="button"
                aria-pressed={mode === m.id}
                title={`${m.label}模式`}
                onClick={() => actions.setMode(m.id)}
              >
                {m.label}
              </button>
            ))}
          </div>
          <span className="grow" />
          {streaming && (
            <button
              type="button"
              className="composer-tool labeled"
              onClick={() => {
                const instruction = window.prompt("Steer");
                if (instruction) void actions.steer(instruction);
              }}
            >
              <IconSteer size={15} />
              调整
            </button>
          )}
          {awaitingTask && (
            <button
              type="button"
              className="composer-tool labeled approval"
              onClick={() => void actions.approveTask(awaitingTask.id)}
            >
              <IconApprove size={15} />
              批准计划
            </button>
          )}
          {streaming ? (
            <button
              type="button"
              className="send stop"
              aria-label="停止生成"
              onClick={() => void actions.cancel()}
            >
              <IconStop size={13} fill="currentColor" />
            </button>
          ) : (
            <button
              type="button"
              className="send"
              disabled={!canSend}
              aria-label="发送"
              onClick={() => void actions.send()}
            >
              <IconSend size={16} />
            </button>
          )}
        </div>
      </div>
      <div className="composer-meta" id="composer-hint">
        <span className="workspace-context" title={workspace.open_path || workspace.project_dir}>
          <IconFolder size={13} />
          {workspace.label}
        </span>
        <span className="hint">
          {disabledReason || "⌘ / Ctrl + Enter 发送 · 内容保存在本机工作区"}
        </span>
      </div>
    </div>
  );
}
