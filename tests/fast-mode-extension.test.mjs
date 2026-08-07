import assert from "node:assert/strict";
import test from "node:test";
import { createExtensionJiti } from "./extension-jiti.mjs";

const jiti = createExtensionJiti(import.meta.url);
const { default: fastMode } = await jiti.import("../pi/extensions/fast-mode/index.ts");

function setup(provider = "openai-codex") {
  const commands = new Map();
  const events = new Map();
  const notices = [];
  const entries = [];
  const pi = {
    registerCommand(name, command) {
      commands.set(name, command);
    },
    on(name, handler) {
      events.set(name, handler);
    },
    appendEntry(customType, data) {
      entries.push({ type: "custom", customType, data });
    },
  };
  const ctx = {
    model: { provider },
    hasUI: true,
    sessionManager: { getBranch() { return entries; } },
    ui: { notify(message, level) { notices.push({ message, level }); } },
  };
  fastMode(pi);
  events.get("session_start")({}, ctx);
  return { commands, events, notices, entries, ctx };
}

test("/fast adds priority service tier only to direct OpenAI requests", async () => {
  const { commands, events, notices, entries, ctx } = setup();
  const command = commands.get("fast");
  const beforeRequest = events.get("before_provider_request");
  assert.ok(command);
  assert.ok(beforeRequest);

  assert.deepEqual(
    await beforeRequest({ payload: { model: "gpt-5.6-luna" } }, ctx),
    { model: "gpt-5.6-luna", service_tier: "priority" },
  );
  await command.handler("off", ctx);
  assert.equal(entries.at(-1).customType, "fast-mode-state");
  assert.equal(entries.at(-1).data.enabled, false);
  assert.equal(await beforeRequest({ payload: { model: "gpt-5.6-luna" } }, ctx), undefined);

  await command.handler("on", ctx);
  assert.equal(entries.at(-1).data.enabled, true);
  entries.pop();
  events.get("session_tree")({}, ctx);
  assert.equal(await beforeRequest({ payload: {} }, ctx), undefined);

  entries.length = 0;
  events.get("session_start")({}, ctx);
  assert.deepEqual(await beforeRequest({ payload: {} }, ctx), { service_tier: "priority" });
});

test("/fast refuses non-OpenAI models", async () => {
  const { commands, events, notices, ctx } = setup("anthropic");
  await commands.get("fast").handler("on", ctx);
  assert.match(notices.at(-1).message, /only for direct OpenAI/);
  assert.equal(await events.get("before_provider_request")({ payload: {} }, ctx), undefined);
});
