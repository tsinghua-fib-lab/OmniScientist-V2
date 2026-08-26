export type Workspace = {
  root: string | null;
  project_dir: string;
  project_name: string;
  invocation_cwd: string;
  kind: string;
  label: string;
  trusted: boolean;
  writable: boolean;
  open_path: string;
  artifacts_dir: string;
  db: string;
};

export type CatalogWorkspace = {
  name: string;
  root: string | null;
  project_dir: string;
  kind: string;
  label: string;
  last_seen: number;
};

export type WorkspaceListResponse = {
  workspaces: CatalogWorkspace[];
  hidden_workspaces?: CatalogWorkspace[];
  hidden_count?: number;
  selected?: Workspace | null;
};

export type WorkspaceVisibilityResponse = WorkspaceListResponse & {
  project_dirs: string[];
};

export type SessionScope = "workspace" | "all";
export type SessionSort = "activity" | "started" | "completed" | "created";
export type SessionStatusGroup =
  | "running"
  | "needs_attention"
  | "completed"
  | "warning"
  | "error"
  | "cancelled"
  | "empty";

export type Session = {
  id: string;
  title: string;
  display_title?: string;
  channel: string;
  status: string;
  messages?: number;
  created_at?: string | null;
  updated_at?: string | null;
  last_activity_at?: string | null;
  first_task_id?: string;
  first_task_at?: string | null;
  latest_task_id?: string;
  latest_task_status?: string;
  latest_task_at?: string | null;
  last_message_id?: string;
  latest_event_seq?: number;
  worker?: string;
  status_group?: SessionStatusGroup;
  message_count?: number;
  project_dir?: string;
  workspace_root?: string | null;
  workspace_kind?: string;
  workspace_label?: string;
  last_message_at?: string | null;
  last_started_at?: string | null;
  last_completed_at?: string | null;
};

export type SessionCatalogError = {
  project_dir?: string;
  workspace_label?: string;
  message: string;
};

export type SessionListAllResponse = {
  sessions: Session[];
  next_cursor?: string | null;
  errors?: SessionCatalogError[];
};

export type SessionDeleteManyResponse = {
  deleted_session_ids: string[];
  deleted_task_ids: string[];
  retained_artifact_count: number;
};

export type ChatMessage = {
  id: string;
  role: string;
  content: string;
  created_at?: string | null;
  content_type?: string;
  name?: string;
  meta?: Record<string, unknown>;
};

export type ActivityItem = {
  task_id: string;
  seq: number;
  timestamp?: string | null;
  kind: string;
  phase: string;
  status: string;
  tool?: string;
  skill?: string;
  workflow_run_id?: string;
  workflow_step_id?: string;
  subtask_id?: string;
  title: string;
  summary: string;
  safe_args?: string;
  safe_result?: string;
  pct?: number | null;
  duration_ms?: number | null;
  error?: string;
  group_key?: string;
  replace_key?: string;
};

export type WorkerState = "live" | "external" | "quiet" | "lost" | "interrupted" | "";

export type TurnStatus = "idle" | "running" | "queued" | "done" | "error";

export type TurnState = {
  workspaceKey: string;
  sessionId: string;
  clientRunId: string;
  taskId: string;
  status: TurnStatus;
  worker: WorkerState;
  partialText: string;
  activities: ActivityItem[];
  lastEventSeq: number;
  error: string;
};

export type DraftState = {
  composer: string;
  mode: Mode;
  attachments: { name: string; uri: string }[];
};

export type TaskSummary = {
  id: string;
  session_id: string;
  parent_task_id: string;
  channel: string;
  status: string;
  kind: string;
  title: string;
  user_input?: string;
  summary: string;
  error: string;
  created_at?: string | null;
  updated_at?: string | null;
  finished_at?: string | null;
};

export type Artifact = {
  id: string;
  session_id: string;
  task_id: string;
  subtask_id?: string;
  workflow_run_id?: string;
  presentation_role?: "primary" | "attachment" | "support" | "process";
  title: string;
  kind: string;
  uri: string;
  rel_path?: string;
  path: string;
  mime: string;
  size_bytes?: number;
  created_at?: string | null;
  preview?: string;
};

export type TaskWorkflow = {
  id: string;
  status: string;
  title?: string;
  goal?: string;
  current_step_id?: string;
  summary?: string;
  error?: string;
  attempt?: number;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  steps?: TaskStep[];
  executions?: TaskExecution[];
};

export type TaskStep = {
  id: string;
  workflow_run_id: string;
  name?: string;
  step_key?: string;
  skill_name?: string;
  status: string;
  position?: number;
  current_execution_id?: string;
  execution_ids?: string[];
  summary?: string;
  error?: string;
  warning?: string;
  executions?: TaskExecution[];
};

export type TaskExecution = {
  id: string;
  task_id?: string;
  workflow_run_id?: string;
  workflow_step_id?: string;
  skill_name?: string;
  skill?: string;
  status: string;
  attempt?: number;
  step_attempt?: number;
  artifact_count?: number;
  summary?: string;
  result_content?: string;
  result_json?: Record<string, unknown>;
  error?: string;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
};

export type TimelineTurn = TaskSummary & {
  executions?: TaskExecution[];
};

export type SessionFingerprint = {
  session_id?: string;
  message_count?: number;
  last_message_id?: string;
  latest_task_id?: string;
  latest_task_status?: string;
  latest_event_seq?: number;
  updated_at?: string | null;
  last_activity_at?: string | null;
};

export type WorkspaceInbox = {
  sessions: Session[];
  focus: (Session & SessionFingerprint) | null;
};

export type SessionTimeline = {
  session: Session;
  messages: ChatMessage[];
  turns: TimelineTurn[];
  fingerprint: SessionFingerprint;
  followable: boolean;
};

export type TaskEvent = {
  id?: string;
  seq?: number;
  event_type?: string;
  kind?: string;
  phase?: string;
  title?: string;
  status?: string;
  name?: string;
  summary?: string;
  workflow_run_id?: string;
  subtask_id?: string;
  created_at?: string | null;
};

export type TaskDetail = {
  task: TaskSummary;
  workflows?: TaskWorkflow[];
  steps?: TaskStep[];
  executions?: TaskExecution[];
  direct_executions?: TaskExecution[];
  subtasks?: TaskExecution[];
  children?: TaskSummary[];
  events?: TaskEvent[];
};

export type DirectoryEntry = {
  name: string;
  path: string;
  is_dir: boolean;
  is_hidden: boolean;
};

export type DirectoryListing = {
  path: string;
  parent: string | null;
  home: string;
  breadcrumbs: { name: string; path: string }[];
  entries: DirectoryEntry[];
};

export type Mode = "auto" | "plan" | "review";
export type Drawer = "none" | "task" | "artifact" | "rom" | "notebook" | "cost";
