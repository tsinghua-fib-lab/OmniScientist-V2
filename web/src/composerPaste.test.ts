import { describe, expect, it } from "vitest";
import { insertTextAtSelection } from "./composerPaste";

describe("insertTextAtSelection", () => {
  it("inserts at the caret without replacing the rest of the draft", () => {
    const next = insertTextAtSelection("hello world", 6, 6, "this ");
    expect(next.value).toBe("hello this world");
    expect(next.caret).toBe(11);
  });

  it("replaces the current selection", () => {
    const next = insertTextAtSelection("hello world", 6, 11, "there");
    expect(next.value).toBe("hello there");
    expect(next.caret).toBe(11);
  });
});
