/**
 * Application-owned conversation viewport with controls pinned to the bottom.
 *
 * Native terminal scrollback cannot keep an interactive editor visible because
 * the application is not notified when that viewport moves. This component
 * instead renders only the visible conversation slice and leaves the prompt,
 * widgets, and footer in a fixed bottom region.
 */

const ANSI_ESCAPE_PATTERN = /\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[()][0-2])/g;

function stripAnsi(text) {
  return text.replace(ANSI_ESCAPE_PATTERN, "");
}

export class PinnedConversationViewport {
  constructor(tui, conversation, controls, onCopy = () => {}) {
    this.tui = tui;
    this.conversation = conversation;
    this.controls = controls;
    this.onCopy = onCopy;
    this.scrollOffset = 0;
    this.maxScrollOffset = 0;
    this.previousConversationHeight = 0;
    this.conversationLines = [];
    this.conversationHeight = 0;
    this.visibleStart = 0;
    this.visibleEnd = 0;
    this.browseMode = false;
    this.browseCursor = 0;
    this.selectionAnchor = null;
    this.pendingG = false;
  }

  scrollBy(lines) {
    if (!Number.isFinite(lines) || lines === 0) return;
    const amount = Math.trunc(lines);
    if (this.browseMode) {
      // Wheel-up reports a positive amount and should move toward older lines.
      this.moveBrowseCursor(-amount);
      return;
    }
    this.scrollOffset = Math.max(
      0,
      Math.min(this.maxScrollOffset, this.scrollOffset + amount),
    );
    this.tui.requestRender();
  }

  scrollToLatest() {
    if (this.scrollOffset === 0) return;
    this.scrollOffset = 0;
    this.tui.requestRender();
  }

  isBrowsing() {
    return this.browseMode;
  }

  enterBrowseMode() {
    if (this.browseMode || this.conversationLines.length === 0) return;
    this.browseMode = true;
    this.selectionAnchor = null;
    this.pendingG = false;
    this.browseCursor = Math.max(0, this.visibleEnd - 1);
    this.ensureBrowseCursorVisible();
    this.tui.requestRender();
  }

  leaveBrowseMode() {
    if (!this.browseMode) return;
    this.browseMode = false;
    this.selectionAnchor = null;
    this.pendingG = false;
    this.tui.requestRender();
  }

  handleBrowseInput(data) {
    if (!this.browseMode) return false;

    if (data === "\x07" || data === "q" || data === "i" || data === "\x1b") {
      this.leaveBrowseMode();
      return true;
    }
    if (data === "\x03") {
      if (this.selectionAnchor !== null) {
        this.selectionAnchor = null;
        this.tui.requestRender();
      } else {
        this.leaveBrowseMode();
      }
      return true;
    }

    if (data === "g") {
      if (this.pendingG) {
        this.pendingG = false;
        this.setBrowseCursor(0);
      } else {
        this.pendingG = true;
      }
      return true;
    }
    this.pendingG = false;

    switch (data) {
      case "G":
        this.setBrowseCursor(this.conversationLines.length - 1);
        return true;
      case "j":
      case "\x1b[B":
        this.moveBrowseCursor(1);
        return true;
      case "k":
      case "\x1b[A":
        this.moveBrowseCursor(-1);
        return true;
      case "\x04": // Ctrl-D
      case "\x1b[6~": // Page Down
        this.moveBrowseCursor(Math.max(1, Math.floor(this.conversationHeight / 2)));
        return true;
      case "\x15": // Ctrl-U
      case "\x1b[5~": // Page Up
        this.moveBrowseCursor(-Math.max(1, Math.floor(this.conversationHeight / 2)));
        return true;
      case "v":
        this.selectionAnchor = this.selectionAnchor === null ? this.browseCursor : null;
        this.tui.requestRender();
        return true;
      case "y":
      case "Y":
        this.copySelection();
        return true;
      default:
        // Browse mode is modal: do not let an unrecognised printable key leak
        // into the pinned editor. Ctrl-G, i, q, or Escape leave the mode.
        return true;
    }
  }

  moveBrowseCursor(delta) {
    this.setBrowseCursor(this.browseCursor + Math.trunc(delta));
  }

  setBrowseCursor(line) {
    if (this.conversationLines.length === 0) return;
    this.browseCursor = Math.max(0, Math.min(this.conversationLines.length - 1, line));
    this.ensureBrowseCursorVisible();
    this.tui.requestRender();
  }

  ensureBrowseCursorVisible() {
    if (!this.browseMode || this.conversationHeight <= 0 || this.conversationLines.length === 0) return;
    let end = this.conversationLines.length - this.scrollOffset;
    let start = Math.max(0, end - this.conversationHeight);
    if (this.browseCursor < start) {
      start = this.browseCursor;
      end = Math.min(this.conversationLines.length, start + this.conversationHeight);
      this.scrollOffset = this.conversationLines.length - end;
    } else if (this.browseCursor >= end) {
      end = Math.min(this.conversationLines.length, this.browseCursor + 1);
      start = Math.max(0, end - this.conversationHeight);
      this.scrollOffset = this.conversationLines.length - end;
    }
    this.scrollOffset = Math.max(0, Math.min(this.maxScrollOffset, this.scrollOffset));
  }

  copySelection() {
    if (this.conversationLines.length === 0) return;
    const start = this.selectionAnchor === null
      ? this.browseCursor
      : Math.min(this.selectionAnchor, this.browseCursor);
    const end = this.selectionAnchor === null
      ? this.browseCursor
      : Math.max(this.selectionAnchor, this.browseCursor);
    const text = this.conversationLines
      .slice(start, end + 1)
      .map(stripAnsi)
      .join("\n")
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n+$/, "");
    if (!text) return;
    try {
      const result = this.onCopy(text);
      if (result && typeof result.catch === "function") {
        result.catch(() => {});
      }
    } catch {
      // Clipboard failures are reported by the caller when it returns a
      // rejected promise; never take down the interactive TUI for a yank.
    }
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
      this.conversationLines = [];
      this.conversationHeight = 0;
      this.visibleStart = 0;
      this.visibleEnd = 0;
      return controlLines.slice(-terminalHeight);
    }

    const conversationLines = this.conversation.render(width);
    const conversationHeight = terminalHeight - controlLines.length;
    const heightDelta = conversationLines.length - this.previousConversationHeight;
    if (this.scrollOffset > 0 && heightDelta > 0) {
      // Preserve the visible passage while streaming appends below it.
      this.scrollOffset += heightDelta;
    }

    this.conversationLines = conversationLines;
    this.conversationHeight = conversationHeight;
    this.maxScrollOffset = Math.max(0, conversationLines.length - conversationHeight);
    this.scrollOffset = Math.max(0, Math.min(this.maxScrollOffset, this.scrollOffset));
    if (this.browseMode) {
      this.browseCursor = Math.max(0, Math.min(conversationLines.length - 1, this.browseCursor));
      this.ensureBrowseCursorVisible();
    }
    const end = Math.max(0, conversationLines.length - this.scrollOffset);
    const start = Math.max(0, end - conversationHeight);
    const visibleConversation = conversationLines.slice(start, end).map((line, index) => {
      const absoluteLine = start + index;
      if (!this.browseMode) return line;
      const selected = this.selectionAnchor === null
        ? absoluteLine === this.browseCursor
        : absoluteLine >= Math.min(this.selectionAnchor, this.browseCursor) &&
          absoluteLine <= Math.max(this.selectionAnchor, this.browseCursor);
      return selected ? `\x1b[7m${line || " "}\x1b[27m` : line;
    });
    const padding = Array(Math.max(0, conversationHeight - visibleConversation.length)).fill("");
    this.previousConversationHeight = conversationLines.length;
    this.visibleStart = start;
    this.visibleEnd = end;
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
