import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Welcome } from "./Welcome";

describe("workspace welcome", () => {
  it("renders the scientist persona feature in Chinese", () => {
    const html = renderToStaticMarkup(createElement(Welcome, { locale: "zh" }));
    expect(html).toContain("从本地工作区继续研究");
    expect(html).toContain("选择学术人格");
  });

  it("renders the scientist persona feature in English", () => {
    const html = renderToStaticMarkup(createElement(Welcome, { locale: "en" }));
    expect(html).toContain("Continue research from a local workspace");
    expect(html).toContain("choose a scientist persona");
    expect(html).not.toContain("学术人格");
  });
});
