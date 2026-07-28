import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Markdown } from "@earendil-works/pi-tui";

type MarkdownToken = {
  type: string;
  depth?: number;
  tokens?: MarkdownToken[];
};

type StyleContext = {
  applyText: (text: string) => string;
  stylePrefix: string;
};

type MarkdownInternals = {
  theme: {
    heading: (text: string) => string;
    bold: (text: string) => string;
    italic: (text: string) => string;
    underline: (text: string) => string;
  };
  getStylePrefix: (style: (text: string) => string) => string;
  renderInlineTokens: (tokens: MarkdownToken[], styleContext: StyleContext) => string;
  renderToken: (token: MarkdownToken, width: number, nextTokenType?: string, styleContext?: unknown) => string[];
};

const ORIGINAL_RENDER_TOKEN = Symbol.for("pi.clean-headings.original-render-token");

type PatchablePrototype = MarkdownInternals & {
  [ORIGINAL_RENDER_TOKEN]?: MarkdownInternals["renderToken"];
};

function installPatch(): void {
  const prototype = Markdown.prototype as unknown as PatchablePrototype;
  if (!prototype[ORIGINAL_RENDER_TOKEN]) prototype[ORIGINAL_RENDER_TOKEN] = prototype.renderToken;
  const original = prototype[ORIGINAL_RENDER_TOKEN]!;

  prototype.renderToken = function cleanHeadingRenderToken(token, width, nextTokenType, styleContext): string[] {
    if (token.type !== "heading") return original.call(this, token, width, nextTokenType, styleContext);

    const level = Math.max(1, Math.min(6, token.depth ?? 1));
    const headingStyle = (text: string): string => {
      if (level === 1) return this.theme.heading(this.theme.bold(this.theme.underline(text)));
      if (level === 2) return this.theme.heading(this.theme.bold(text));
      if (level === 3) return this.theme.heading(this.theme.bold(this.theme.italic(text)));
      if (level === 4) return this.theme.heading(this.theme.underline(text));
      if (level === 5) return this.theme.heading(this.theme.italic(text));
      return this.theme.heading(text);
    };
    const headingContext: StyleContext = {
      applyText: headingStyle,
      stylePrefix: this.getStylePrefix(headingStyle),
    };
    const heading = this.renderInlineTokens(token.tokens ?? [], headingContext);
    return nextTokenType && nextTokenType !== "space" ? [heading, ""] : [heading];
  };
}

export default function cleanHeadings(_pi: ExtensionAPI): void {
  installPatch();
}
