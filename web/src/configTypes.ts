export type ConfigRow = {
  key: string;
  value: unknown;
  secret: boolean;
  set: boolean;
};

export type ConfigCatalogItem = {
  key: string;
  label: string;
  roles: string[];
  default_endpoint: string;
  default_model: string;
};

export type ConfigDescribe = {
  setup_required: boolean;
  notice?: string;
  restart_required?: boolean;
  paths: Record<string, string>;
  home: {
    active: string;
    source: string;
    default: string;
    selection_file: string;
    environment_override?: boolean;
    changed?: boolean;
    restart_required?: boolean;
    message?: string;
    warning?: string;
    notes?: string[];
    notice?: string;
  };
  catalog: ConfigCatalogItem[];
  rows: ConfigRow[];
  mcp_servers: {
    name: string;
    command: string;
    url: string;
    enabled: boolean;
    args: string[];
  }[];
  blocks: {
    model: {
      provider: string;
      base_url: string;
      model: string;
      api_key_set: boolean;
      health: string;
      health_detail: string;
    };
    vlm: {
      enabled: boolean;
      model: string;
      endpoint: string;
      protocol: string;
      timeout_s: number;
      api_key_set: boolean;
    };
    semantic_scholar: { api_key_set: boolean; enabled: boolean };
    embeddings: {
      enabled: boolean;
      provider: string;
      base_url: string;
      model: string;
      api_key_set: boolean;
      specter2_python: string;
      specter2_base_model: string;
      specter2_adapter: string;
      specter2_device: string;
    };
    memory: { enabled: boolean; embeddings_enabled: boolean };
    react: Record<string, number | boolean>;
    display: { ui_mode: string; verbosity: string };
    cost: Record<string, number | boolean>;
    tasks: Record<string, number | boolean>;
    schedules: { enabled: boolean };
    security: {
      bash_sandbox: string;
      require_approval: boolean;
      approval_policy: string;
      approval_allowlist: string[];
    };
    channels: { enabled: string[] };
    skills: {
      sources: string[];
      disabled: string[];
      default_for: Record<string, string>;
      export_targets: string[];
      max_prompt_iterations: number;
      max_prompt_tool_calls: number;
      max_prompt_seconds: number;
      max_python_seconds: number;
      max_cli_seconds: number;
    };
  };
};

export type ConfigWriteResult = {
  notice?: string;
  changed?: string[];
  message?: string;
  restart_required?: boolean;
};

export const MAIN_PROVIDERS = ["openai", "openai_compatible", "deepseek", "ollama", "mock"] as const;
