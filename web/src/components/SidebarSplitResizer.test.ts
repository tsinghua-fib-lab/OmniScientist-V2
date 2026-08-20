import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { SidebarSplitResizer } from "./SidebarSplitResizer";

describe("SidebarSplitResizer", () => {
  it("exposes an accessible keyboard-adjustable horizontal separator", () => {
    const html = renderToStaticMarkup(
      createElement(SidebarSplitResizer, {
        controlsId: "workspace-section session-section",
        value: 238,
        min: 96,
        max: 471,
        defaultValue: 238,
        onChange: vi.fn(),
        onCommit: vi.fn(),
      }),
    );

    expect(html).toContain('role="separator"');
    expect(html).toContain('aria-orientation="horizontal"');
    expect(html).toContain('aria-controls="workspace-section session-section"');
    expect(html).toContain('aria-valuemin="96"');
    expect(html).toContain('aria-valuemax="471"');
    expect(html).toContain('aria-valuenow="238"');
    expect(html).toContain('aria-valuetext="工作区高度 238 像素"');
    expect(html).toContain('tabindex="0"');
    expect(html.match(/<span aria-hidden="true"><\/span>/g)).toHaveLength(1);
  });
});
