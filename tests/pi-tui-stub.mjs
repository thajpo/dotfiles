export const Key = {
  ctrl(key) { return `ctrl+${key}`; },
};

export function matchesKey(data, key) {
  const aliases = {
    escape: ["escape", "\u001b"],
    "ctrl+c": ["ctrl+c", "\u0003"],
    tab: ["tab", "\t"],
    up: ["up", "\u001b[A"],
    down: ["down", "\u001b[B"],
    pageUp: ["pageUp"],
    pageDown: ["pageDown"],
  };
  return (aliases[key] ?? [key]).includes(data);
}

export function visibleWidth(value) {
  return [...String(value)].length;
}

export function truncateToWidth(value, width, ellipsis = "") {
  const text = String(value);
  if (visibleWidth(text) <= width) return text;
  const suffix = visibleWidth(ellipsis) <= width ? ellipsis : "";
  return [...text].slice(0, Math.max(0, width - visibleWidth(suffix))).join("") + suffix;
}

export function wrapTextWithAnsi(value, width) {
  const lines = [];
  for (const sourceLine of String(value).split("\n")) {
    if (!sourceLine) {
      lines.push("");
      continue;
    }
    let remaining = sourceLine;
    while (remaining.length > width) {
      let split = remaining.lastIndexOf(" ", width);
      if (split <= 0) split = width;
      lines.push(remaining.slice(0, split));
      remaining = remaining.slice(split).trimStart();
    }
    lines.push(remaining);
  }
  return lines;
}
