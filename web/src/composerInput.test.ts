import { describe, expect, it, vi } from "vitest";
import { handleComposerKeyDown } from "./composerInput";

type KeyOptions = {
  key?: string;
  shiftKey?: boolean;
  metaKey?: boolean;
  ctrlKey?: boolean;
  repeat?: boolean;
  legacyKeyCode?: number;
};

function keyEvent(options: KeyOptions = {}) {
  return {
    key: options.key ?? "Enter",
    shiftKey: options.shiftKey ?? false,
    metaKey: options.metaKey ?? false,
    ctrlKey: options.ctrlKey ?? false,
    repeat: options.repeat ?? false,
    preventDefault: vi.fn(),
  };
}

function dispatch(
  options: KeyOptions = {},
  state: { canSend?: boolean; isComposing?: boolean } = {},
) {
  const event = keyEvent(options);
  const send = vi.fn();
  handleComposerKeyDown(event, {
    canSend: state.canSend ?? true,
    isComposing: state.isComposing ?? false,
    legacyKeyCode: options.legacyKeyCode,
    send,
  });
  return { event, send };
}

describe("handleComposerKeyDown", () => {
  it("sends on Enter", () => {
    const { event, send } = dispatch();

    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(send).toHaveBeenCalledOnce();
  });

  it.each([{ metaKey: true }, { ctrlKey: true }])(
    "keeps the existing modified-Enter send shortcut",
    (modifiers) => {
      const { event, send } = dispatch(modifiers);

      expect(event.preventDefault).toHaveBeenCalledOnce();
      expect(send).toHaveBeenCalledOnce();
    },
  );

  it("leaves Shift+Enter to the textarea as a native newline", () => {
    const { event, send } = dispatch({ shiftKey: true, metaKey: true });

    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
  });

  it("does not send while an IME composition is active", () => {
    const { event, send } = dispatch({}, { isComposing: true });

    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
  });

  it("does not send when an IME reports the legacy composition key code", () => {
    const { event, send } = dispatch({ legacyKeyCode: 229 });

    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
  });

  it("consumes a held Enter without repeatedly sending", () => {
    const { event, send } = dispatch({ repeat: true });

    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(send).not.toHaveBeenCalled();
  });

  it("consumes Enter without sending when the draft cannot be submitted", () => {
    const { event, send } = dispatch({}, { canSend: false });

    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(send).not.toHaveBeenCalled();
  });

  it("ignores non-Enter keys", () => {
    const { event, send } = dispatch({ key: "a" });

    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
  });
});
