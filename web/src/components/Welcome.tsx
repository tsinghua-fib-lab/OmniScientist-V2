import { IconFolderOpen, IconPersona } from "../icons";
import { actions } from "../store";
import type { LocalePreference } from "../uiPrefs";

export function Welcome({ locale = "zh" }: { locale?: LocalePreference }) {
  const en = locale === "en";
  return (
    <div className="welcome">
      <div className="welcome-content">
        <p className="eyebrow">OmniScientist Web</p>
        <h1>{en ? "Continue research from a local workspace" : "从本地工作区继续研究"}</h1>
        <p>
          {en
            ? "Choose a local directory to continue the same conversations, tasks, notes, and research artifacts as the CLI."
            : "选择一个本机目录，即可继续 CLI 中的会话、任务、笔记和研究产物。"}
        </p>
        <button type="button" className="btn" onClick={() => void actions.openPicker()}>
          <IconFolderOpen size={16} />
          {en ? "Open directory" : "打开目录"}
        </button>
        <div className="welcome-feature">
          <IconPersona size={16} />
          <span>
            {en
              ? "After opening a folder, choose a scientist persona to shape the next ReAct turn's judgment and expression."
              : "打开文件夹后，可选择学术人格，让下一轮进入 ReAct 的判断与表达贴合不同科学家的方法品味。"}
          </span>
        </div>
        <p className="welcome-note">
          {en
            ? "All data stays local. The Web UI is another surface over the same Omni agent."
            : "所有数据保存在本机，网页只是同一套 Omni 智能体的界面。"}
        </p>
      </div>
    </div>
  );
}
