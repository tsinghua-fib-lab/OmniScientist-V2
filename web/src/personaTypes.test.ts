import { describe, expect, it } from "vitest";
import type { PersonaSnapshot, PersonaStartRequest } from "./personaTypes";
import { personaOperationOutcome } from "./personaTypes";

const active: PersonaSnapshot = {
  active: true,
  scientist_id: "fengli-xu",
  scientist_name: "Fengli Xu",
  scanner: "home",
  writable: true,
  available: [],
  invalid: [],
  operation: null,
};

const refresh: PersonaStartRequest = {
  action: "refresh",
  scientist_id: "fengli-xu",
  task_context: "review memory architecture",
};

describe("persona operation settlement", () => {
  it("does not complete a refresh from the unchanged pre-task snapshot", () => {
    expect(personaOperationOutcome("running", active, refresh)).toBe("pending");
  });

  it("treats an empty outcome code as success when the snapshot already matches", () => {
    const activate: PersonaStartRequest = {
      action: "activate",
      scientist_id: "fengli-xu",
    };
    expect(personaOperationOutcome("succeeded", active, activate)).toBe("succeeded");
    expect(personaOperationOutcome("succeeded", active, activate, "unchanged_task")).toBe(
      "succeeded",
    );
    expect(
      personaOperationOutcome("succeeded", active, activate, "no_scientific_task"),
    ).toBe("failed");
  });

  it("requires a successful Task, SoulAgent outcome, and requested final snapshot", () => {
    expect(personaOperationOutcome("failed", active, refresh, "refreshed")).toBe("failed");
    expect(personaOperationOutcome("succeeded", active, refresh, "refreshed")).toBe(
      "succeeded",
    );
    expect(
      personaOperationOutcome(
        "succeeded",
        active,
        refresh,
        "no_scientific_task",
      ),
    ).toBe("failed");
    expect(
      personaOperationOutcome(
        "succeeded",
        { ...active, active: false },
        refresh,
        "refreshed",
      ),
    ).toBe("failed");
  });

  it("settles when the SoulAgent skill finishes even if the parent is still running", () => {
    expect(
      personaOperationOutcome("running", active, refresh, "refreshed", "succeeded"),
    ).toBe("succeeded");
    expect(
      personaOperationOutcome("running", active, refresh, "refreshed", "running"),
    ).toBe("pending");
  });

  it("settles unload only after the overlay is absent", () => {
    const unload: PersonaStartRequest = { action: "unload" };
    expect(personaOperationOutcome("running", { ...active, active: false }, unload)).toBe(
      "pending",
    );
    expect(
      personaOperationOutcome(
        "running",
        { ...active, active: false },
        unload,
        "unloaded",
        "succeeded",
      ),
    ).toBe("succeeded");
    expect(
      personaOperationOutcome(
        "succeeded",
        { ...active, active: false },
        unload,
        "unloaded",
      ),
    ).toBe("succeeded");
  });
});
