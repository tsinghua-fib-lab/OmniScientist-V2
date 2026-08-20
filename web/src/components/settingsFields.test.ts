import { describe, expect, it } from "vitest";
import { formatValue, mainModelReady, parseAdvancedValue, vlmHasValues } from "./settingsFields";

describe("mainModelReady", () => {
  it("accepts mock without a URL", () => {
    expect(mainModelReady({ provider: "mock", base_url: "", model: "omni-mock", api_key: "" })).toBe(true);
  });

  it("requires URL and model for a real provider", () => {
    expect(mainModelReady({ provider: "openai", base_url: "", model: "gpt-4o-mini", api_key: "" })).toBe(
      false,
    );
    expect(
      mainModelReady({
        provider: "openai",
        base_url: "https://api.openai.com/v1",
        model: "gpt-4o-mini",
        api_key: "",
      }),
    ).toBe(true);
  });
});

describe("vlmHasValues", () => {
  it("treats an empty optional block as skipped", () => {
    expect(
      vlmHasValues({
        enabled: false,
        endpoint: "",
        model: "",
        api_key: "",
        protocol: "openai_compatible_chat",
        timeout_s: "",
      }),
    ).toBe(false);
  });
});

describe("parseAdvancedValue", () => {
  it("keeps JSON-looking strings for the same coerce path as omni config set", () => {
    expect(parseAdvancedValue("true")).toBe("true");
    expect(parseAdvancedValue("[\"cli\"]")).toBe("[\"cli\"]");
    expect(parseAdvancedValue("deepseek-chat")).toBe("deepseek-chat");
  });
});

describe("formatValue", () => {
  it("renders lists as JSON", () => {
    expect(formatValue(["cli", "web"])).toBe('["cli","web"]');
  });
});
