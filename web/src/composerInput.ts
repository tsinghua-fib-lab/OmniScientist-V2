export type ComposerKeyEvent = {
  key: string;
  shiftKey: boolean;
  repeat: boolean;
  preventDefault: () => void;
};

export type ComposerKeyOptions = {
  canSend: boolean;
  isComposing: boolean;
  legacyKeyCode?: number;
  send: () => void | Promise<void>;
};

/**
 * Apply the chat composer keyboard contract without depending on the DOM.
 *
 * Shift+Enter remains a native textarea newline. IME confirmation also stays
 * native, including the legacy keyCode=229 signal still emitted by some
 * browser/input-method combinations.
 */
export function handleComposerKeyDown(
  event: ComposerKeyEvent,
  options: ComposerKeyOptions,
): void {
  if (event.key !== "Enter" || event.shiftKey) return;
  if (options.isComposing || options.legacyKeyCode === 229) return;

  event.preventDefault();
  if (event.repeat || !options.canSend) return;
  void options.send();
}
