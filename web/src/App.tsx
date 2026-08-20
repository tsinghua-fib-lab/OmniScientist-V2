import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { Chat } from "./components/Chat";
import { Composer } from "./components/Composer";
import { DirectoryModal } from "./components/DirectoryModal";
import { Drawers, InspectorTabs } from "./components/Drawers";
import { Onboarding } from "./components/Onboarding";
import { PaneResizer } from "./components/PaneResizer";
import { Settings } from "./components/Settings";
import { Sidebar } from "./components/Sidebar";
import { Welcome } from "./components/Welcome";
import { displayTitle } from "./format";
import {
  IconMaximize,
  IconMenu,
  IconMinimize,
  IconPanelLeftOpen,
  IconPersona,
  IconSettings,
} from "./icons";
import {
  DEFAULT_PANE_LAYOUT,
  PANEL_MAX_WIDTH,
  PANEL_MIN_WIDTH,
  PANE_LAYOUT_STORAGE_KEY,
  SIDEBAR_MAX_WIDTH,
  SIDEBAR_MIN_WIDTH,
  clampPaneWidth,
  maxPanelWidth,
  maxSidebarWidth,
  parsePaneLayout,
  serializePaneLayout,
  type PaneLayoutPreferences,
} from "./paneLayout";
import { ApiError, api } from "./api";
import { channelSummary } from "./channelStatus";
import type { ChannelDescribeResponse } from "./channelTypes";
import { personaOperationOutcome } from "./personaTypes";
import type {
  PersonaAction,
  PersonaSnapshot,
  PersonaStartRequest,
  PersonaStatusResponse,
} from "./personaTypes";
import { actions, useAppState } from "./store";
import type { Drawer } from "./types";
import {
  applyLocale,
  applyTheme,
  readUiPrefs,
  writeUiPrefs,
  type UiPrefs,
} from "./uiPrefs";

type LayoutMode = "normal" | "main-focus" | "panel-fullscreen";

function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

function useViewportWidth() {
  const [width, setWidth] = useState(() => window.innerWidth);
  useEffect(() => {
    let frame = 0;
    const update = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => setWidth(window.innerWidth));
    };
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("resize", update);
      window.cancelAnimationFrame(frame);
    };
  }, []);
  return width;
}

function readPanePreferences(): PaneLayoutPreferences {
  try {
    return parsePaneLayout(window.localStorage.getItem(PANE_LAYOUT_STORAGE_KEY));
  } catch {
    return { ...DEFAULT_PANE_LAYOUT };
  }
}

function persistPanePreferences(value: PaneLayoutPreferences) {
  try {
    window.localStorage.setItem(PANE_LAYOUT_STORAGE_KEY, serializePaneLayout(value));
  } catch {
    // The layout remains usable when storage is unavailable or disabled.
  }
}

export function App() {
  const { workspace, error, notice, sessionId, sessions, drawer, streaming } = useAppState();
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [layoutMode, setLayoutMode] = useState<LayoutMode>("normal");
  const [uiPrefs, setUiPrefs] = useState<UiPrefs>(readUiPrefs);
  const [setupRequired, setSetupRequired] = useState<boolean | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [channelData, setChannelData] = useState<ChannelDescribeResponse | null>(null);
  const [channelStatusAvailable, setChannelStatusAvailable] = useState(true);
  const handleChannelsChanged = useCallback((data: ChannelDescribeResponse) => {
    setChannelData(data);
    setChannelStatusAvailable(true);
  }, []);
  const [panePreferences, setPanePreferences] =
    useState<PaneLayoutPreferences>(readPanePreferences);
  const [persona, setPersona] = useState<PersonaSnapshot | null>(null);
  const [personaLoading, setPersonaLoading] = useState(false);
  const [personaBusy, setPersonaBusy] = useState(false);
  const [personaError, setPersonaError] = useState("");
  const [personaNotice, setPersonaNotice] = useState("");
  const [pendingPersonaId, setPendingPersonaId] = useState("");
  const [pendingPersonaAction, setPendingPersonaAction] = useState<PersonaAction | "">("");
  const personaRequestRef = useRef(0);
  const personaOperationRef = useRef(0);
  const personaBusyRef = useRef(false);
  const personaBroadcastRef = useRef<BroadcastChannel | null>(null);
  const [resizing, setResizing] = useState(false);
  const appRef = useRef<HTMLDivElement>(null);
  const navigationToggleRef = useRef<HTMLButtonElement>(null);
  const settingsTriggerRef = useRef<HTMLElement | null>(null);
  const inspectorTriggerRef = useRef<HTMLButtonElement | null>(null);
  const inspectorReturnTabRef = useRef<Exclude<Drawer, "none"> | null>(null);
  const navigationOverlay = useMediaQuery("(max-width: 840px)");
  const panelOverlay = useMediaQuery("(max-width: 1279px)");
  const viewportWidth = useViewportWidth();
  const mainFocus = layoutMode === "main-focus";
  const panelFullscreen = layoutMode === "panel-fullscreen";
  const panelOpen = drawer !== "none";
  const panelOpenRef = useRef(panelOpen);
  panelOpenRef.current = panelOpen;
  const panelVisible = panelOpen && !mainFocus;
  const desktopSidebarCollapsed = !navigationOverlay && panePreferences.sidebarCollapsed;
  const sidebarVisible = !desktopSidebarCollapsed && !mainFocus && !panelFullscreen;
  const panelDocked = panelVisible && !panelOverlay && !panelFullscreen;
  const navigationModalOpen = navigationOverlay && navigationOpen && !panelFullscreen;
  const panelModalOpen = panelVisible && (panelOverlay || panelFullscreen);
  const session = sessions.find((item) => item.id === sessionId);
  const personaWorkspace = workspace?.open_path || workspace?.project_dir || "";
  const personaWorkspaceRef = useRef(personaWorkspace);
  personaWorkspaceRef.current = personaWorkspace;

  const reloadPersona = useCallback(async (silent = false) => {
    const owner = personaWorkspaceRef.current;
    const requestId = ++personaRequestRef.current;
    if (!owner) {
      setPersona(null);
      setPersonaLoading(false);
      setPersonaError("");
      return;
    }
    if (!silent) setPersonaLoading(true);
    try {
      const data = await api.describePersona(owner);
      if (requestId !== personaRequestRef.current || owner !== personaWorkspaceRef.current) return;
      setPersona(data.persona);
      setPersonaError("");
    } catch (err) {
      if (requestId !== personaRequestRef.current || owner !== personaWorkspaceRef.current) return;
      setPersonaError(err instanceof Error ? err.message : String(err));
    } finally {
      if (requestId === personaRequestRef.current && owner === personaWorkspaceRef.current) {
        setPersonaLoading(false);
      }
    }
  }, []);

  const followPersonaTask = useCallback(async (
    owner: string,
    taskId: string,
    request: PersonaStartRequest,
    operationId: number,
  ) => {
    let statusWasUnavailable = false;
    for (let attempt = 0; ; attempt += 1) {
      if (operationId !== personaOperationRef.current || owner !== personaWorkspaceRef.current) return;
      if (attempt > 0) {
        await new Promise<void>((resolve) => window.setTimeout(resolve, 1800));
      }
      let status: PersonaStatusResponse;
      try {
        status = await api.personaStatus(owner, taskId);
      } catch (err) {
        if (operationId !== personaOperationRef.current || owner !== personaWorkspaceRef.current) return;
        if (
          err instanceof ApiError &&
          ["not_found", "invalid_params", "forbidden", "untrusted"].includes(err.code)
        ) {
          const data = await api.describePersona(owner);
          if (operationId !== personaOperationRef.current || owner !== personaWorkspaceRef.current) return;
          setPersona(data.persona);
          throw err;
        }
        statusWasUnavailable = true;
        setPersonaNotice(
          uiPrefs.locale === "en"
            ? "Persona status is temporarily unavailable. Controls stay locked while Omni reconnects."
            : "暂时无法读取人格任务状态；重新连通前将继续锁定相关操作。",
        );
        continue;
      }
      if (operationId !== personaOperationRef.current || owner !== personaWorkspaceRef.current) return;
      if (statusWasUnavailable) {
        statusWasUnavailable = false;
        setPersonaNotice("");
      }
      const data = await api.describePersona(owner);
      if (operationId !== personaOperationRef.current || owner !== personaWorkspaceRef.current) return;
      setPersona(data.persona);
      const outcome = personaOperationOutcome(
        status.task_status,
        data.persona,
        request,
        status.outcome_code,
        status.skill_status || "",
      );
      if (outcome === "pending") {
        if (attempt === 56) {
          setPersonaNotice(
            uiPrefs.locale === "en"
              ? "SoulAgent is still writing the persona. Settings stay locked; you can send research messages."
              : "SoulAgent 仍在写入人格；设置中的人格操作保持锁定，但不影响发送研究消息。",
          );
        }
        continue;
      }
      if (outcome === "succeeded") {
        const expectsActive = request.action !== "unload";
        setPersonaNotice(
          uiPrefs.locale === "en"
            ? expectsActive
              ? `${data.persona.scientist_name} is active for the next ReAct turn.`
              : "Standard Omni has been restored."
            : expectsActive
              ? `${data.persona.scientist_name} 已启用，下一轮进入 ReAct 时生效。`
              : "已恢复标准 Omni。",
        );
        personaBroadcastRef.current?.postMessage({ type: "persona.changed", workspace: owner });
        return;
      }
      throw new Error(
        uiPrefs.locale === "en"
          ? "SoulAgent finished without applying the requested persona change."
          : "SoulAgent 已结束，但未应用请求的人格变更。",
      );
    }
  }, [uiPrefs.locale]);

  const startPersona = useCallback(async (request: PersonaStartRequest) => {
    const owner = personaWorkspaceRef.current;
    if (!owner || personaBusyRef.current) return;
    if (streaming) {
      setPersonaError(
        uiPrefs.locale === "en"
          ? "Wait for the current task to finish before changing the scientist persona."
          : "请等待当前任务结束后再调整学术人格。",
      );
      return;
    }
    personaBusyRef.current = true;
    const operationId = ++personaOperationRef.current;
    setPersonaBusy(true);
    setPersonaError("");
    setPersonaNotice("");
    setPendingPersonaId(request.scientist_id || "");
    setPendingPersonaAction(request.action);
    try {
      const started = await api.startPersona(owner, request);
      if (operationId !== personaOperationRef.current || owner !== personaWorkspaceRef.current) return;
      personaBroadcastRef.current?.postMessage({ type: "persona.changed", workspace: owner });
      await followPersonaTask(
        owner,
        started.task_id,
        request,
        operationId,
      );
    } catch (err) {
      if (operationId === personaOperationRef.current && owner === personaWorkspaceRef.current) {
        let failure: unknown = err;
        if (err instanceof ApiError && err.code === "busy") {
          try {
            const data = await api.describePersona(owner);
            if (operationId !== personaOperationRef.current || owner !== personaWorkspaceRef.current) return;
            setPersona(data.persona);
            const operation = data.persona.operation;
            const busyTaskId = String(err.extra?.task_id || "");
            if (operation && (!busyTaskId || operation.task_id === busyTaskId)) {
              const existingRequest: PersonaStartRequest = {
                action: operation.action,
                ...(operation.scientist_id
                  ? { scientist_id: operation.scientist_id }
                  : {}),
                ...(operation.action === "refresh" ? { force: true } : {}),
              };
              setPendingPersonaId(operation.scientist_id);
              setPendingPersonaAction(operation.action);
              await followPersonaTask(
                owner,
                operation.task_id,
                existingRequest,
                operationId,
              );
              return;
            }
            setPersonaNotice(
              uiPrefs.locale === "en"
                ? "Another persona operation just finished. Review the current state before retrying."
                : "另一个人格操作刚刚结束；请确认当前状态后再重试。",
            );
            return;
          } catch (resumeError) {
            failure = resumeError;
          }
        }
        setPersonaError(failure instanceof Error ? failure.message : String(failure));
      }
    } finally {
      if (operationId === personaOperationRef.current && owner === personaWorkspaceRef.current) {
        personaBusyRef.current = false;
        setPersonaBusy(false);
        setPendingPersonaId("");
        setPendingPersonaAction("");
      }
    }
  }, [followPersonaTask, streaming, uiPrefs.locale]);

  const restoreInspectorFocus = useCallback(() => {
    const trigger = inspectorTriggerRef.current;
    const returnTab = inspectorReturnTabRef.current;
    window.requestAnimationFrame(() => {
      if (trigger?.isConnected) {
        trigger.focus();
        return;
      }
      if (returnTab) {
        document
          .querySelector<HTMLButtonElement>(`.topbar [data-inspector-tab="${returnTab}"]`)
          ?.focus();
      }
    });
  }, []);

  const unclampedSidebarWidth = clampPaneWidth(
    panePreferences.sidebarWidth,
    SIDEBAR_MIN_WIDTH,
    SIDEBAR_MAX_WIDTH,
  );
  const effectiveSidebarWidth = navigationOverlay
    ? unclampedSidebarWidth
    : clampPaneWidth(
        unclampedSidebarWidth,
        SIDEBAR_MIN_WIDTH,
        maxSidebarWidth(viewportWidth, 0, false),
      );
  const panelResizeMax = panelDocked
    ? maxPanelWidth(viewportWidth, effectiveSidebarWidth, sidebarVisible)
    : PANEL_MAX_WIDTH;
  const effectivePanelWidth = clampPaneWidth(
    panePreferences.panelWidth,
    PANEL_MIN_WIDTH,
    panelResizeMax,
  );
  const sidebarResizeMax = maxSidebarWidth(
    viewportWidth,
    effectivePanelWidth,
    panelDocked,
  );
  const appStyle = {
    "--sidebar-width": `${effectiveSidebarWidth}px`,
    "--panel-width": `${effectivePanelWidth}px`,
  } as CSSProperties;

  useEffect(() => {
    applyTheme(uiPrefs.theme);
    applyLocale(uiPrefs.locale);
    writeUiPrefs(uiPrefs);
  }, [uiPrefs]);

  useEffect(() => {
    if (uiPrefs.theme !== "system") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const sync = () => applyTheme("system");
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, [uiPrefs.theme]);

  useEffect(() => {
    personaOperationRef.current += 1;
    personaBusyRef.current = false;
    setPersona(null);
    setPersonaError("");
    setPersonaNotice("");
    setPersonaBusy(false);
    setPendingPersonaId("");
    setPendingPersonaAction("");
    void reloadPersona();
  }, [personaWorkspace, reloadPersona]);

  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const channel = new BroadcastChannel("omni-scientist-persona");
    personaBroadcastRef.current = channel;
    channel.onmessage = (event: MessageEvent<unknown>) => {
      const message = event.data;
      if (
        typeof message === "object" &&
        message !== null &&
        "type" in message &&
        "workspace" in message &&
        message.type === "persona.changed" &&
        message.workspace === personaWorkspaceRef.current
      ) {
        void reloadPersona(true);
      }
    };
    return () => {
      if (personaBroadcastRef.current === channel) personaBroadcastRef.current = null;
      channel.close();
    };
  }, [reloadPersona]);

  useEffect(() => {
    const operation = persona?.operation;
    const owner = personaWorkspaceRef.current;
    if (!owner || !operation?.task_id || personaBusyRef.current) return;

    const request: PersonaStartRequest = {
      action: operation.action,
      ...(operation.scientist_id ? { scientist_id: operation.scientist_id } : {}),
      ...(operation.action === "refresh" ? { force: true } : {}),
    };
    personaBusyRef.current = true;
    const operationId = ++personaOperationRef.current;
    setPersonaBusy(true);
    setPersonaError("");
    setPersonaNotice(
      uiPrefs.locale === "en"
        ? "Resuming the active SoulAgent task…"
        : "正在继续跟踪进行中的 SoulAgent 任务…",
    );
    setPendingPersonaId(operation.scientist_id);
    setPendingPersonaAction(operation.action);

    void followPersonaTask(owner, operation.task_id, request, operationId)
      .catch((err: unknown) => {
        if (operationId === personaOperationRef.current && owner === personaWorkspaceRef.current) {
          setPersonaError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (operationId === personaOperationRef.current && owner === personaWorkspaceRef.current) {
          personaBusyRef.current = false;
          setPersonaBusy(false);
          setPendingPersonaId("");
          setPendingPersonaAction("");
        }
      });
  }, [
    followPersonaTask,
    persona?.operation?.task_id,
    personaBusy,
    personaWorkspace,
    uiPrefs.locale,
  ]);

  useEffect(() => {
    if (!personaWorkspace) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible" && !personaBusyRef.current) {
        void reloadPersona(true);
      }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [personaWorkspace, reloadPersona]);

  useEffect(() => {
    if (!personaWorkspace || streaming) return;
    void reloadPersona(true);
  }, [personaWorkspace, reloadPersona, streaming]);

  useEffect(() => {
    if (!personaWorkspace) return;
    const refreshVisiblePersona = () => {
      if (document.visibilityState === "visible") void reloadPersona(true);
    };
    window.addEventListener("focus", refreshVisiblePersona);
    document.addEventListener("visibilitychange", refreshVisiblePersona);
    return () => {
      window.removeEventListener("focus", refreshVisiblePersona);
      document.removeEventListener("visibilitychange", refreshVisiblePersona);
    };
  }, [personaWorkspace, reloadPersona]);

  useEffect(() => {
    if (settingsOpen && uiPrefs.lastSection === "personas") void reloadPersona(true);
  }, [settingsOpen, uiPrefs.lastSection, reloadPersona]);

  useEffect(() => {
    void api
      .describeConfig()
      .then((data) => setSetupRequired(data.setup_required))
      .catch(() => setSetupRequired(false));
  }, []);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const data = await api.describeChannels();
        if (active) {
          setChannelData(data);
          setChannelStatusAvailable(true);
        }
      } catch {
        if (active) setChannelStatusAvailable(false);
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 12000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const onPop = () => {
      void actions.restoreFromHash();
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    if (!workspace) return;
    let active = true;
    const synchronize = () => {
      if (!active || document.visibilityState === "hidden") return;
      void actions.syncWorkspace();
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") synchronize();
    };
    synchronize();
    const timer = window.setInterval(synchronize, 1800);
    window.addEventListener("focus", synchronize);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", synchronize);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [workspace?.project_dir, workspace?.open_path, sessionId]);

  useEffect(() => {
    if (navigationOverlay) {
      setLayoutMode((mode) => (mode === "main-focus" ? "normal" : mode));
    } else {
      setNavigationOpen(false);
    }
  }, [navigationOverlay]);

  useEffect(() => {
    if (drawer === "none") {
      setLayoutMode((mode) => (mode === "panel-fullscreen" ? "normal" : mode));
    }
  }, [drawer]);

  useEffect(() => {
    if (!navigationModalOpen) return;
    const frame = window.requestAnimationFrame(() => {
      document.querySelector<HTMLElement>(".sidebar")?.focus();
    });
    return () => {
      window.cancelAnimationFrame(frame);
      navigationToggleRef.current?.focus();
    };
  }, [navigationModalOpen]);

  useEffect(() => {
    if (!panelModalOpen) return;
    const frame = window.requestAnimationFrame(() => {
      const panel = document.querySelector<HTMLElement>("[data-testid='inspector-panel']");
      if (panel && !panel.contains(document.activeElement)) panel.focus();
    });
    return () => {
      window.cancelAnimationFrame(frame);
      if (!panelOpenRef.current) restoreInspectorFocus();
    };
  }, [panelModalOpen, restoreInspectorFocus]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (
        event.key !== "Escape" ||
        event.defaultPrevented ||
        resizing ||
        document.querySelector(".modal-backdrop")
      ) {
        return;
      }
      if (panelFullscreen || mainFocus) setLayoutMode("normal");
      else if (navigationOpen) setNavigationOpen(false);
      else if (panelOpen) {
        void actions.openDrawer("none");
        restoreInspectorFocus();
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [mainFocus, navigationOpen, panelFullscreen, panelOpen, resizing, restoreInspectorFocus]);

  const previewSidebarWidth = (sidebarWidth: number) => {
    appRef.current?.style.setProperty("--sidebar-width", `${sidebarWidth}px`);
  };

  const commitSidebarWidth = (sidebarWidth: number) => {
    setPanePreferences((current) => {
      const next = { ...current, sidebarWidth };
      persistPanePreferences(next);
      return next;
    });
  };

  const previewPanelWidth = (panelWidth: number) => {
    appRef.current?.style.setProperty("--panel-width", `${panelWidth}px`);
  };

  const commitPanelWidth = (panelWidth: number) => {
    setPanePreferences((current) => {
      const next = { ...current, panelWidth };
      persistPanePreferences(next);
      return next;
    });
  };

  const setPaneResizing = (active: boolean) => {
    appRef.current?.classList.toggle("is-resizing", active);
    setResizing(active);
  };

  const setSidebarCollapsed = (sidebarCollapsed: boolean) => {
    setPanePreferences((current) => {
      const next = { ...current, sidebarCollapsed };
      persistPanePreferences(next);
      return next;
    });
  };

  const collapsePanel = () => {
    setLayoutMode("normal");
    void actions.openDrawer("none");
    restoreInspectorFocus();
  };

  const openSettings = (section?: UiPrefs["lastSection"]) => {
    const trigger = document.activeElement;
    if (trigger instanceof HTMLElement && !trigger.closest(".settings-modal")) {
      settingsTriggerRef.current = trigger;
    }
    if (section) setUiPrefs((current) => ({ ...current, lastSection: section }));
    setSettingsOpen(true);
    setNavigationOpen(false);
  };

  const closeSettings = () => {
    setSettingsOpen(false);
    const trigger = settingsTriggerRef.current;
    settingsTriggerRef.current = null;
    window.requestAnimationFrame(() => {
      if (trigger?.isConnected) trigger.focus();
    });
  };

  const title = workspace
    ? session
      ? displayTitle(session)
      : workspace.label
    : "Omni";
  const appClasses = [
    "app",
    panelVisible ? "with-panel" : "",
    navigationOpen ? "navigation-open" : "",
    desktopSidebarCollapsed ? "sidebar-collapsed" : "",
    mainFocus ? "main-focus" : "",
    panelFullscreen ? "panel-fullscreen" : "",
    resizing ? "is-resizing" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div ref={appRef} className={appClasses} style={appStyle}>
      <Sidebar
        onNavigate={() => setNavigationOpen(false)}
        onCollapse={() => {
          setSidebarCollapsed(true);
          window.requestAnimationFrame(() => navigationToggleRef.current?.focus());
        }}
        overlay={navigationOverlay}
        open={navigationOpen}
        blocked={panelModalOpen}
        hidden={desktopSidebarCollapsed || mainFocus || panelFullscreen}
        onOpenSettings={() => openSettings()}
        onOpenChannels={() => openSettings("channels")}
        channelSummary={channelData ? channelSummary(channelData) : undefined}
        channelStatusAvailable={channelStatusAvailable}
        locale={uiPrefs.locale}
        settingsLabel={uiPrefs.locale === "en" ? "Settings" : "设置"}
      />
      {navigationModalOpen && (
        <div
          className="shell-backdrop navigation-backdrop"
          aria-hidden="true"
          onClick={() => setNavigationOpen(false)}
        />
      )}
      {!navigationOverlay &&
        sidebarVisible &&
        !panelModalOpen &&
        sidebarResizeMax > SIDEBAR_MIN_WIDTH && (
        <PaneResizer
          side="left"
          label="调整左侧导航宽度"
          controlsId="workspace-sidebar"
          value={effectiveSidebarWidth}
          min={SIDEBAR_MIN_WIDTH}
          max={sidebarResizeMax}
          defaultValue={DEFAULT_PANE_LAYOUT.sidebarWidth}
          onChange={previewSidebarWidth}
          onCommit={commitSidebarWidth}
          onDraggingChange={setPaneResizing}
        />
      )}
      <main
        id="main-workspace"
        className="main"
        hidden={panelFullscreen}
        inert={navigationModalOpen || panelModalOpen ? "" : undefined}
      >
        <header className="topbar">
          <button
            ref={navigationToggleRef}
            type="button"
            className={`icon-btn square navigation-toggle${
              desktopSidebarCollapsed ? " desktop-visible" : ""
            }`}
            aria-label={desktopSidebarCollapsed ? "展开侧栏" : "打开导航"}
            aria-controls="workspace-sidebar"
            aria-expanded={navigationOverlay ? navigationOpen : !desktopSidebarCollapsed}
            title={desktopSidebarCollapsed ? "展开侧栏" : "打开导航"}
            onClick={() => {
              if (desktopSidebarCollapsed) {
                setSidebarCollapsed(false);
                window.requestAnimationFrame(() => {
                  document.querySelector<HTMLButtonElement>(".sidebar-collapse")?.focus();
                });
                return;
              }
              if (panelOpen) collapsePanel();
              setNavigationOpen(true);
            }}
          >
            {desktopSidebarCollapsed ? <IconPanelLeftOpen size={18} /> : <IconMenu size={18} />}
          </button>
          <div className="title-cluster grow">
            <h2 title={title}>{title}</h2>
            {workspace && session && <span className="topbar-context">{workspace.label}</span>}
          </div>
          {workspace ? (
            <button
              type="button"
              className={`persona-chip${persona?.active ? " active" : ""}${personaBusy ? " busy" : ""}`}
              aria-label={
                persona?.active
                  ? uiPrefs.locale === "en"
                    ? `Current scientist persona: ${persona.scientist_name}`
                    : `当前学术人格 ${persona.scientist_name}`
                  : uiPrefs.locale === "en"
                    ? "Choose scientist persona"
                    : "选择学术人格"
              }
              title={
                persona?.active
                  ? `${persona.scientist_name} · ${uiPrefs.locale === "en" ? "current folder" : "当前文件夹"}`
                  : uiPrefs.locale === "en"
                    ? "Choose scientist persona"
                    : "选择学术人格"
              }
              onClick={() => openSettings("personas")}
            >
              <IconPersona size={14} />
              <span>
                {persona?.active
                  ? persona.scientist_name
                  : uiPrefs.locale === "en"
                    ? "Persona"
                    : "学术人格"}
              </span>
            </button>
          ) : null}
          {session && <span className={`badge ${session.channel}`}>{session.channel}</span>}
          {desktopSidebarCollapsed && (
            <button
              type="button"
              className="icon-btn square"
              aria-label="设置"
              title="设置"
              onClick={() => openSettings()}
            >
              <IconSettings size={17} />
            </button>
          )}
          {workspace && !navigationOverlay && (
            <button
              type="button"
              className="icon-btn square main-focus-toggle"
              aria-label={mainFocus ? "退出专注模式" : "专注主内容"}
              aria-controls="main-workspace"
              aria-pressed={mainFocus}
              title={mainFocus ? "退出专注模式" : "专注主内容"}
              onClick={() => {
                setNavigationOpen(false);
                setLayoutMode(mainFocus ? "normal" : "main-focus");
              }}
            >
              {mainFocus ? <IconMinimize size={16} /> : <IconMaximize size={16} />}
            </button>
          )}
          {workspace && !panelOpen && (
            <InspectorTabs
              onSelect={(tab, trigger) => {
                inspectorTriggerRef.current = trigger;
                inspectorReturnTabRef.current = tab;
                setNavigationOpen(false);
                setLayoutMode("normal");
                void actions.openDrawer(tab);
              }}
            />
          )}
        </header>
        {error && (
          <div className="banner error" role="alert">
            {error}
          </div>
        )}
        {notice && !error && (
          <div className="banner" role="status">
            {notice}
          </div>
        )}
        {workspace ? (
          <Chat
            locale={uiPrefs.locale}
            persona={persona}
            personaLoading={personaLoading}
            personaBusy={personaBusy || streaming}
            personaError={personaError}
            personaNotice={personaNotice}
            personaFolderPath={workspace.open_path || workspace.invocation_cwd || ""}
            pendingPersonaId={pendingPersonaId}
            pendingPersonaAction={pendingPersonaAction}
            onPersonaStart={startPersona}
            onManagePersonas={() => openSettings("personas")}
            onOpenTaskArtifacts={(taskId, trigger) => {
              inspectorTriggerRef.current = trigger;
              inspectorReturnTabRef.current = "artifact";
              setNavigationOpen(false);
              setLayoutMode("normal");
              void actions.showTaskArtifacts(taskId);
            }}
          />
        ) : (
          <Welcome locale={uiPrefs.locale} />
        )}
        {workspace ? (
          <Composer />
        ) : null}
      </main>
      {panelModalOpen && !panelFullscreen && (
        <div
          className="shell-backdrop panel-backdrop"
          aria-hidden="true"
          onClick={collapsePanel}
        />
      )}
      {panelDocked && panelResizeMax > PANEL_MIN_WIDTH && (
        <PaneResizer
          side="right"
          label="调整右侧检查器宽度"
          controlsId="workspace-inspector"
          value={effectivePanelWidth}
          min={PANEL_MIN_WIDTH}
          max={panelResizeMax}
          defaultValue={DEFAULT_PANE_LAYOUT.panelWidth}
          onChange={previewPanelWidth}
          onCommit={commitPanelWidth}
          onDraggingChange={setPaneResizing}
        />
      )}
      <Drawers
        overlay={(panelOverlay || panelFullscreen) && !mainFocus}
        fullscreen={panelFullscreen}
        hidden={mainFocus}
        onClose={collapsePanel}
        onToggleFullscreen={() => {
          setNavigationOpen(false);
          setLayoutMode(panelFullscreen ? "normal" : "panel-fullscreen");
        }}
      />
      <DirectoryModal />
      {setupRequired ? (
        <Onboarding
          locale={uiPrefs.locale}
          onComplete={async () => {
            const data = await api.describeConfig();
            setSetupRequired(data.setup_required);
          }}
        />
      ) : null}
      {settingsOpen && !setupRequired ? (
        <Settings
          prefs={uiPrefs}
          onPrefs={setUiPrefs}
          onChannelsChanged={handleChannelsChanged}
          persona={persona}
          personaLoading={personaLoading}
          personaBusy={personaBusy || streaming}
          personaWorkspaceKey={personaWorkspace}
          personaFolderPath={workspace?.open_path || workspace?.invocation_cwd || ""}
          personaError={personaError}
          personaNotice={personaNotice}
          pendingPersonaId={pendingPersonaId}
          pendingPersonaAction={pendingPersonaAction}
          onPersonaReload={() => reloadPersona()}
          onPersonaStart={startPersona}
          onClose={closeSettings}
        />
      ) : null}
    </div>
  );
}
