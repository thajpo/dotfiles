import assert from "node:assert/strict";
import test from "node:test";
import { createJiti } from "../pi/npm/node_modules/jiti/lib/jiti.mjs";

const jiti = createJiti(import.meta.url);
const { default: fastMode } = await jiti.import("../pi/extensions/fast-mode/index.ts");

function setup(provider = "openai-codex") {
  const commands = new Map();
  const events = new Map();
  const notices = [];
  const pi = {
    registerCommand(name, command) {
      commands.set(name, command);
    },
    on(name, handler) {
      events.set(name, handler);
    },
  };
  const ctx = {
    model: { provider },
    ui: { notify(message, level) { notices.push({ message, level }); } },
  };
  fastMode(pi);
  return { commands, events, notices, ctx };
}

test("/fast adds priority service tier only to direct OpenAI requests", async () => {
  const { commands, events, notices, ctx } = setup();
  const command = commands.get("fast");
  const beforeRequest = events.get("before_provider_request");
  assert.ok(command);
  assert.ok(beforeRequest);

  assert.equal(await beforeRequest({ payload: { model: "gpt-5.6-luna" } }, ctx), undefined);
  await command.handler("on", ctx);
  assert.match(notices.at(-1).message, /Fast mode: ON/);
  assert.deepEqual(
    await beforeRequest({ payload: { model: "gpt-5.6-luna" } }, ctx),
    { model: "gpt-5.6-luna", service_tier: "priority" },
  );

  await command.handler("off", ctx);
  assert.equal(await beforeRequest({ payload: { model: "gpt-5.6-luna" } }, ctx), undefined);
});

test("/fast refuses non-OpenAI models", async () => {
  const { commands, events, notices, ctx } = setup("anthropic");
  await commands.get("fast").handler("on", ctx);
  assert.match(notices.at(-1).message, /only for direct OpenAI/);
  assert.equal(await events.get("before_provider_request")({ payload: {} }, ctx), undefined);
});
