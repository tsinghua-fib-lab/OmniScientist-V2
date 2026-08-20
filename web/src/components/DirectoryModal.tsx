import { useEffect, useRef } from "react";
import { trapFocus } from "../focus";
import { IconChevronUp, IconClose, IconFolder } from "../icons";
import { actions, useAppState } from "../store";

export function DirectoryModal() {
  const { pickerOpen, picker, showHidden } = useAppState();
  const dialogRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!pickerOpen) return;
    previousFocus.current = document.activeElement as HTMLElement | null;
    window.requestAnimationFrame(() => dialogRef.current?.focus());
    return () => previousFocus.current?.focus();
  }, [pickerOpen]);

  if (!pickerOpen || !picker) return null;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={actions.closePicker}>
      <div
        ref={dialogRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="dir-title"
        aria-describedby="dir-description"
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            actions.closePicker();
            return;
          }
          trapFocus(event);
        }}
      >
        <header>
          <div>
            <h2 id="dir-title">选择工作区目录</h2>
            <p className="muted" id="dir-description">
              目录与 CLI cwd 对齐，使用同一份会话和产物。
            </p>
          </div>
          <button type="button" className="icon-btn square" aria-label="关闭目录选择器" onClick={actions.closePicker}>
            <IconClose size={17} />
          </button>
        </header>
        <div className="directory-location">
          <div className="crumbs" aria-label="当前目录层级">
            {picker.breadcrumbs.map((crumb) => (
              <button key={crumb.path} type="button" onClick={() => void actions.openPicker(crumb.path)}>
                {crumb.name}
              </button>
            ))}
          </div>
          <code title={picker.path}>{picker.path}</code>
        </div>
        <div className="entries">
          {picker.parent && (
            <button type="button" className="entry" onClick={() => void actions.openPicker(picker.parent || undefined)}>
              <span className="row">
                <IconChevronUp size={15} />
                上一级
              </span>
              <span className="muted">{picker.parent}</span>
            </button>
          )}
          {picker.entries
            .filter((entry) => entry.is_dir)
            .map((entry) => (
              <button
                key={entry.path}
                type="button"
                className="entry"
                onClick={() => void actions.openPicker(entry.path)}
              >
                <span className="row">
                  <IconFolder size={15} />
                  {entry.name}
                </span>
                <span className="muted">打开</span>
              </button>
            ))}
        </div>
        <footer>
          <label className="row muted">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => void actions.setShowHidden(e.target.checked)}
            />
            显示隐藏
          </label>
          <span className="grow" />
          <button type="button" className="btn ghost" onClick={actions.closePicker}>
            取消
          </button>
          <button type="button" className="btn" onClick={() => void actions.openWorkspace(picker.path)}>
            打开
          </button>
        </footer>
      </div>
    </div>
  );
}
