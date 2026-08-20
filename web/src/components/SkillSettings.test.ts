import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ConfigDescribe } from "../configTypes";
import { settingsCopy } from "../settingsCopy";
import type { SkillSummary } from "../skillTypes";
import { SkillSettings, visibleSkillActions } from "./SkillSettings";

const describeConfig = {
  mcp_servers: [],
} as unknown as ConfigDescribe;

const builtin: SkillSummary = {
  skill_id: "builtin:arxiv-fetch",
  name: "arxiv-fetch",
  source: "builtin",
  description: "Fetch an arXiv paper",
  kind: "python_engine",
  delivery_mode: "sync_tool",
  version: "1.0.0",
  license: "MIT",
  trusted: true,
  origin: "",
  active: true,
  shadowed: false,
  shadowed_by: "",
  allow_implicit: true,
  can_trust: false,
  can_untrust: false,
  can_remove: false,
};

const imported: SkillSummary = {
  skill_id: "user_omni:demo",
  name: "demo",
  source: "user_omni",
  description: "Imported demo skill",
  kind: "prompt_only",
  delivery_mode: "sync_tool",
  version: "",
  license: "MIT",
  trusted: false,
  origin: "/tmp/demo",
  active: true,
  shadowed: false,
  shadowed_by: "",
  allow_implicit: true,
  can_trust: true,
  can_untrust: false,
  can_remove: true,
};

function render(skills: SkillSummary[], selected?: string) {
  return renderToStaticMarkup(
    createElement(SkillSettings, {
      copy: settingsCopy("zh"),
      describe: describeConfig,
      busy: false,
      run: async () => undefined,
      initialSkills: skills,
      initialSelected: selected,
    }),
  );
}

describe("SkillSettings", () => {
  it("splits Skills and MCP and no longer exposes raw skills.toml fields", () => {
    const html = render([builtin]);
    expect(html).toContain('data-settings-tab="skills"');
    expect(html).toContain('data-settings-tab="mcp"');
    expect(html).toContain("技能");
    expect(html).toContain("MCP");
    expect(html).not.toContain("skills.disabled");
    expect(html).not.toContain("skills.sources");
    expect(html).not.toContain("skills.export_targets");
  });

  it("does not offer delete or trust on a builtin skill", () => {
    const html = render([builtin, imported], "builtin:arxiv-fetch");
    expect(html).toContain("arxiv-fetch");
    expect(html).not.toContain('data-skill-action="remove"');
    expect(html).not.toContain('data-skill-action="trust"');
    expect(visibleSkillActions(builtin)).toEqual([]);
  });

  it("offers delete on an imported user skill", () => {
    const html = render([builtin, imported], "user_omni:demo");
    expect(html).toContain('data-skill-action="remove"');
    expect(html).toContain('data-skill-action="trust"');
    expect(visibleSkillActions(imported)).toEqual(["trust", "remove"]);
  });
});
