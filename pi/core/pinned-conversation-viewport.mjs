/**
 * Application-owned conversation viewport with controls pinned to the bottom.
 *
 * Native terminal scrollback cannot keep an interactive editor visible because
 * the application is not notified when that viewport moves. This component
 * instead renders only the visible conversation slice and leaves the prompt,
 * widgets, and footer in a fixed bottom region.
 */
export class PinnedConversationViewport {
  constructor(tui, conversation, controls) {
    this.tui = tui;
    this.conversation = conversation;
    this.controls = controls;
    this.scrollOffset = 0;
    this.maxScrollOffset = 0;
    this.previousConversationHeight = 0;
  }

  scrollBy(lines) {
    if (!Number.isFinite(lines) || lines === 0) return;
    this.scrollOffset = Math.max(
      0,
      Math.min(this.maxScrollOffset, this.scrollOffset + Math.trunc(lines)),
    );
    this.tui.requestRender();
  }

  scrollToLatest() {
    if (this.scrollOffset === 0) return;
    this.scrollOffset = 0;
    this.tui.requestRender();
  }

  invalidate() {
    this.conversation.invalidate();
    this.controls.invalidate();
  }

  render(width) {
    const terminalHeight = Math.max(1, this.tui.terminal.rows);
    const controlLines = this.controls.render(width);
    if (controlLines.length >= terminalHeight) {
      this.scrollOffset = 0;
      this.maxScrollOffset = 0;
      this.previousConversationHeight = 0;
      return controlLines.slice(-terminalHeight);
    }

    const conversationLines = this.conversation.render(width);
    const conversationHeight = terminalHeight - controlLines.length;
    const heightDelta = conversationLines.length - this.previousConversationHeight;
    if (this.scrollOffset > 0 && heightDelta > 0) {
      // Preserve the visible passage while streaming appends below it.
      this.scrollOffset += heightDelta;
    }

    this.maxScrollOffset = Math.max(0, conversationLines.length - conversationHeight);
    this.scrollOffset = Math.max(0, Math.min(this.scrollOffset, this.maxScrollOffset));
    const end = Math.max(0, conversationLines.length - this.scrollOffset);
    const start = Math.max(0, end - conversationHeight);
    const visibleConversation = conversationLines.slice(start, end);
    const padding = Array(Math.max(0, conversationHeight - visibleConversation.length)).fill("");
    this.previousConversationHeight = conversationLines.length;
    return [...visibleConversation, ...padding, ...controlLines];
  }
}

const MOUSE_INPUT_PATTERN = /\x1b\[<(\d+);\d+;\d+[Mm]/g;

export function stripMouseInput(data) {
  return data.replace(MOUSE_INPUT_PATTERN, "");
}

export function isMouseInput(data) {
  return data.length > 0 && stripMouseInput(data).length === 0;
}

export function parseWheelDirection(data) {
  let direction = 0;
  for (const match of data.matchAll(MOUSE_INPUT_PATTERN)) {
    const button = Number.parseInt(match[1], 10);
    if ((button & 64) === 0) continue;
    const wheelButton = button & 3;
    if (wheelButton === 0) direction += 1;
    if (wheelButton === 1) direction -= 1;
  }
  return direction;
}
