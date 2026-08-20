import { describe, expect, it, vi } from "vitest";
import { createLiveResizeSession } from "./resizeInteraction";

describe("live resize interaction", () => {
  it("previews every pointer sample synchronously and commits only the final value", () => {
    const onPreview = vi.fn();
    const onCommit = vi.fn();
    const session = createLiveResizeSession({
      startCoordinate: 100,
      startValue: 288,
      min: 232,
      max: 440,
      direction: 1,
      onPreview,
      onCommit,
    });

    expect(session.move(140)).toBe(328);
    expect(onPreview).toHaveBeenLastCalledWith(328);
    expect(onCommit).not.toHaveBeenCalled();

    expect(session.move(160)).toBe(348);
    expect(onPreview).toHaveBeenLastCalledWith(348);
    expect(onCommit).not.toHaveBeenCalled();

    expect(session.finish(160)).toBe(348);
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith(348);
  });

  it("supports reverse movement and clamps the live preview to its bounds", () => {
    const onPreview = vi.fn();
    const session = createLiveResizeSession({
      startCoordinate: 500,
      startValue: 352,
      min: 320,
      max: 720,
      direction: -1,
      onPreview,
      onCommit: vi.fn(),
    });

    expect(session.move(470)).toBe(382);
    expect(session.move(-500)).toBe(720);
    expect(session.move(600)).toBe(320);
  });

  it("restores the starting value on cancel without committing", () => {
    const onPreview = vi.fn();
    const onCommit = vi.fn();
    const session = createLiveResizeSession({
      startCoordinate: 200,
      startValue: 238,
      min: 96,
      max: 470,
      direction: 1,
      onPreview,
      onCommit,
    });

    session.move(280);
    expect(session.cancel()).toBe(238);
    expect(onPreview).toHaveBeenLastCalledWith(238);
    expect(onCommit).not.toHaveBeenCalled();
    expect(session.move(300)).toBeNull();
  });
});
