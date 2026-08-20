export type SkillSource = "builtin" | "user_omni";

export type SkillSummary = {
  skill_id: string;
  name: string;
  source: SkillSource;
  description: string;
  kind: string;
  delivery_mode: string;
  version: string;
  license: string;
  trusted: boolean;
  origin: string;
  active: boolean;
  shadowed: boolean;
  shadowed_by: string;
  allow_implicit: boolean;
  can_trust: boolean;
  can_untrust: boolean;
  can_remove: boolean;
};

export type SkillDetail = SkillSummary & {
  when_to_use: string;
  body: string;
  path: string;
  allowed_tools: string[];
  requires_bins: string[];
  requires_env: string[];
  capabilities: string[];
  executable_files: string[];
};

export type SkillListResponse = {
  skills: SkillSummary[];
  notice?: string;
  restart_required?: boolean;
};

export type SkillMutationResponse = {
  name: string;
  skill_id: string;
  status: string;
  dest?: string;
  executable_files?: string[];
  notice?: string;
};
