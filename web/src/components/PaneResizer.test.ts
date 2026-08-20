import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { PaneResizer } from "./PaneResizer";

describe("PaneResizer", () => {
  it("exposes its current bounds and keyboard-adjustable separator role", () => {
    const html = renderToStaticMarkup(
      createElement(PaneResizer, {
        side: "left",
        label: "调整左侧导航宽度",
        controlsId: "workspace-sidebar",
        value: 288,
        min: 232,
        max: 440,
        defaultValue: 288,
        onChange: vi.fn(),
        onCommit: vi.fn(),
      }),
    );

    expect(html).toContain('role="separator"');
    expect(html).toContain('aria-orientation="vertical"');
    expect(html).toContain('aria-controls="workspace-sidebar"');
    expect(html).toContain('aria-valuemin="232"');
    expect(html).toContain('aria-valuemax="440"');
    expect(html).toContain('aria-valuenow="288"');
    expect(html).toContain('aria-valuetext="288 像素"');
    expect(html).toContain('tabindex="0"');
    expect(html.match(/<span aria-hidden="true"><\/span>/g)).toHaveLength(1);
  });
});
