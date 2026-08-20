import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { PersonaSnapshot } from "../personaTypes";
import { PersonaQuickStart, PersonaSettings } from "./PersonaSettings";

const persona: PersonaSnapshot = {
  active: true,
  scientist_id: "fengli-xu",
  scientist_name: "Fengli Xu",
  scanner: "home",
  writable: true,
  available: [
    {
      scientist_id: "fengli-xu",
      scientist_name: "Fengli Xu",
      aliases: ["徐丰力"],
    },
    {
      scientist_id: "kaiming-he",
      scientist_name: "Kaiming He",
      aliases: ["何恺明"],
    },
  ],
  invalid: [],
  operation: null,
};

describe("scientist persona controls", () => {
  it("keeps the empty-conversation quick picker compact and folder-scoped", () => {
    const html = renderToStaticMarkup(
      createElement(PersonaQuickStart, {
        locale: "zh",
        snapshot: persona,
        loading: false,
        busy: false,
        onStart: async () => undefined,
        onManage: () => undefined,
      }),
    );
    expect(html).toContain("学术人格");
    expect(html).toContain("Fengli Xu");
    expect(html).toContain("当前文件夹");
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain("恢复标准 Omni");
    expect(html).toContain("管理全部");
    expect(html).not.toContain("当前科研任务");
    expect(html).not.toContain("按当前任务刷新");
    expect(html).not.toContain("role.md");
  });

  it("keeps an active persona visible when the catalog has more than four entries", () => {
    const expanded: PersonaSnapshot = {
      ...persona,
      scientist_id: "richard-feynman",
      scientist_name: "Richard Feynman",
      available: [
        ...persona.available,
        { scientist_id: "alan-turing", scientist_name: "Alan Turing", aliases: [] },
        { scientist_id: "claude-shannon", scientist_name: "Claude Shannon", aliases: [] },
        { scientist_id: "john-von-neumann", scientist_name: "John von Neumann", aliases: [] },
        { scientist_id: "richard-feynman", scientist_name: "Richard Feynman", aliases: [] },
      ],
    };
    const html = renderToStaticMarkup(
      createElement(PersonaQuickStart, {
        locale: "en",
        snapshot: expanded,
        loading: false,
        busy: false,
        onStart: async () => undefined,
        onManage: () => undefined,
      }),
    );

    expect(html).toContain("Richard Feynman");
    expect(html).toContain('aria-pressed="true"');
    expect((html.match(/persona-avatar/g) || []).length).toBeLessThanOrEqual(4);
  });

  it("renders a searchable settings catalog and reversible active state", () => {
    const html = renderToStaticMarkup(
      createElement(PersonaSettings, {
        locale: "zh",
        snapshot: persona,
        loading: false,
        busy: false,
        onReload: async () => undefined,
        onStart: async () => undefined,
      }),
    );
    expect(html).toContain('role="group"');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain("搜索科学家");
    expect(html).toContain("重新读取");
    expect(html).toContain("恢复标准 Omni");
    expect(html).toContain("不改变工具、安全和引用规则");
    expect(html).toContain("子文件夹不会继承");
    expect(html).not.toContain("当前科研任务");
    expect(html).not.toContain("按当前任务刷新");
    expect(html).not.toContain("当前工作区");
  });

  it("shows the opened folder path and does not ask for a research task", () => {
    const html = renderToStaticMarkup(
      createElement(PersonaSettings, {
        locale: "zh",
        snapshot: persona,
        loading: false,
        busy: false,
        folderPath: "/repo/papers",
        onReload: async () => undefined,
        onStart: async () => undefined,
      }),
    );
    expect(html).toContain("/repo/papers");
    expect(html).toContain("已打开的文件夹");
    expect(html).toContain("研究任务请启用后在下方输入框发送");
    expect(html).toContain("已启用");
    expect(html).not.toContain("当前科研任务");
  });

  it("does not claim activation while a request is still pending", () => {
    const inactive = { ...persona, active: false, scientist_id: "", scientist_name: "" };
    const html = renderToStaticMarkup(
      createElement(PersonaSettings, {
        locale: "en",
        snapshot: inactive,
        loading: false,
        busy: true,
        pendingScientistId: "kaiming-he",
        onReload: async () => undefined,
        onStart: async () => undefined,
      }),
    );
    expect(html).toContain("Configuring this folder with SoulAgent");
    expect(html).toContain("You can still send research messages");
    expect(html).not.toContain("A task is running in this conversation");
    expect(html).not.toContain("Active in this folder");
  });
});
