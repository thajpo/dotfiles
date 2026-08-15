'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { createJiti } = require('../../../npm/node_modules/jiti');

function canonical(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
}

function readFrame(socket) {
  return new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0);
    const onError = (error) => {
      socket.off('data', onData);
      reject(error);
    };
    const onData = (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      const newline = buffer.indexOf(0x0a);
      if (newline < 0) return;
      socket.off('data', onData);
      socket.off('error', onError);
      try { resolve(JSON.parse(buffer.subarray(0, newline).toString('utf8'))); }
      catch (error) { reject(error); }
    };
    socket.on('data', onData);
    socket.once('error', onError);
  });
}

function sendFrame(socket, value) {
  socket.write(canonical(value) + '\n');
}

(async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-web-bridge-'));
  const manifestPath = path.join(root, 'manifest.json');
  const runId = 'run_' + '1'.repeat(32);
  const conversationId = 'conv_' + '2'.repeat(32);
  const projectId = 'prj_' + '3'.repeat(32);
  fs.writeFileSync(manifestPath, JSON.stringify({
    runId,
    conversation: { conversationId },
    project: { projectId },
    installedBuild: { buildId: 'build-run' },
    manifestDigest: 'sha256:' + '5'.repeat(64)
  }));
  const previous = {
    PI_SYSTEM_STATE_ROOT: process.env.PI_SYSTEM_STATE_ROOT,
    PI_RUNTIME_MANIFEST: process.env.PI_RUNTIME_MANIFEST,
     PI_CONTROLLER_BUILD_ID: process.env.PI_CONTROLLER_BUILD_ID,
    PI_CONTROLLER_RESTART_EPOCH: process.env.PI_CONTROLLER_RESTART_EPOCH
  };
  Object.assign(process.env, {
    PI_SYSTEM_STATE_ROOT: root,
    PI_RUNTIME_MANIFEST: manifestPath,
     PI_CONTROLLER_BUILD_ID: 'build-controller',
    PI_CONTROLLER_RESTART_EPOCH: 'ctl_' + '4'.repeat(32)
  });

  const handlers = new Map();
  const sent = [];
  const entries = [];
  const queued = [];
  let leafId = 'root';
  let idle = true;
  let rejectNext = false;
  let selectedModel = { provider: 'test', id: 'model-1', name: 'Test model' };
  let thinkingLevel = 'medium';
  const pi = {
    on(name, handler) { handlers.set(name, handler); },
    appendEntry(customType, data) {
      leafId = 'marker-' + (entries.length + 1);
      entries.push({ type: 'custom', id: leafId, customType, data });
    },
    sendUserMessage(text, options) {
      sent.push({ text, options });
      if (!idle && options && options.deliverAs) {
        queued.push({ id: options.queueId, text, deliverAs: options.deliverAs });
      }
      const accepted = !rejectNext;
      rejectNext = false;
      options && options.preflightResult && options.preflightResult(accepted);
    },
    getCommands: () => [{ name: 'example', description: 'Example command', source: 'prompt' }],
    getThinkingLevel: () => thinkingLevel,
    setThinkingLevel: (level) => { thinkingLevel = level; },
    setModel: async (model) => { selectedModel = model; return true; }
  };
  const jiti = createJiti(process.cwd());
  const extension = await jiti.import('./pi/extensions/web-session/index.ts');
  extension.default(pi);
  const context = {
    isIdle: () => idle,
    hasPendingMessages: () => queued.length > 0,
    getQueuedMessages: () => queued.map((item) => ({ ...item })),
    removeQueuedMessage: (id) => {
      const index = queued.findIndex((item) => item.id === id);
      if (index < 0) return false;
      queued.splice(index, 1);
      return true;
    },
    model: selectedModel,
    modelRegistry: { getAvailable: () => [selectedModel], find: () => selectedModel },
    sessionManager: {
      getSessionId: () => 'pi-' + conversationId,
      getEntries: () => entries,
      getLeafId: () => leafId
    }
  };
  handlers.get('session_start')({ type: 'session_start' }, context);

  const descriptorPath = path.join(root, 'web-bridges', runId + '.json');
  for (let attempt = 0; attempt < 50 && !fs.existsSync(descriptorPath); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 10));
  assert.ok(fs.existsSync(descriptorPath), 'bridge descriptor created');
  const descriptor = JSON.parse(fs.readFileSync(descriptorPath, 'utf8'));
  assert.equal(descriptor.protocolVersion, 2);
  assert.equal(descriptor.controllerBuildId, 'build-controller');
  assert.equal(descriptor.runBuildId, 'build-run');
  assert.equal(descriptor.manifestDigest, 'sha256:' + '5'.repeat(64));
  assert.equal(descriptor.childPid, process.pid);
  assert.equal(typeof descriptor.childStartIdentity, 'string');
  const socket = net.createConnection(descriptor.socketPath);
  await new Promise((resolve, reject) => { socket.once('connect', resolve); socket.once('error', reject); });
  sendFrame(socket, {
     protocolVersion: 2,
    type: 'connect',
    runId,
    conversationId,
    projectId,
    sessionId: descriptor.sessionId,
     controllerBuildId: descriptor.controllerBuildId,
     runBuildId: descriptor.runBuildId,
     manifestDigest: descriptor.manifestDigest,
     childPid: descriptor.childPid,
     childStartIdentity: descriptor.childStartIdentity,
     restartEpoch: descriptor.restartEpoch,
     capability: descriptor.capability
  });
  assert.equal((await readFrame(socket)).type, 'connected');
  sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-state', operation: 'state' });
  const runtime = await readFrame(socket);
  assert.equal(runtime.type, 'state');
   assert.equal(runtime.state.idle, true);
   assert.equal(runtime.state.thinkingLevel, 'medium');
   assert.deepEqual(runtime.state.queued, []);
  sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-idle', idempotencyKey: 'input-idle', operation: 'prompt', text: 'hello while idle' });
  assert.equal((await readFrame(socket)).type, 'accepted');
  sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-thinking', idempotencyKey: 'setting-1', operation: 'setThinking', thinkingLevel: 'high' });
  assert.equal((await readFrame(socket)).type, 'accepted');
  assert.equal(thinkingLevel, 'high');
  sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-model', idempotencyKey: 'setting-2', operation: 'setModel', model: 'test/model-1' });
  assert.equal((await readFrame(socket)).type, 'accepted');
  assert.equal(selectedModel.id, 'model-1');
   sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-1', idempotencyKey: 'input-1', operation: 'prompt', text: 'hello from browser', deliverAs: 'followUp' });
   assert.equal((await readFrame(socket)).type, 'accepted');
   assert.deepEqual(sent.map((item) => ({
     text: item.text,
     deliverAs: item.options && item.options.deliverAs,
     queueId: item.options && item.options.queueId,
     hasPreflight: Boolean(item.options && item.options.preflightResult),
   })), [
     { text: 'hello while idle', deliverAs: undefined, queueId: undefined, hasPreflight: true },
     { text: 'hello from browser', deliverAs: 'followUp', queueId: undefined, hasPreflight: true }
   ]);
  sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-1-retry', idempotencyKey: 'input-1', operation: 'prompt', text: 'hello from browser', deliverAs: 'followUp' });
  assert.equal((await readFrame(socket)).type, 'accepted');
  assert.equal(sent.length, 2, 'same idempotency key is delivered once');
  sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-1-conflict', idempotencyKey: 'input-1', operation: 'prompt', text: 'different text', deliverAs: 'followUp' });
   const conflict = await readFrame(socket);
   assert.equal(conflict.type, 'rejected');
   assert.equal(conflict.error.code, 'CP_IDEMPOTENCY_CONFLICT');
   rejectNext = true;
   sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-rejected', idempotencyKey: 'input-rejected', operation: 'prompt', text: 'rejected before delivery' });
   const rejected = await readFrame(socket);
   assert.equal(rejected.type, 'rejected');
   assert.equal(rejected.error.code, 'CP_INPUT_REJECTED');
   sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-rejected-retry', idempotencyKey: 'input-rejected', operation: 'prompt', text: 'rejected before delivery' });
   assert.equal((await readFrame(socket)).error.code, 'CP_INPUT_REJECTED');
  sendFrame(socket, { protocolVersion: 2, type: 'subscribe', requestId: 'web-2' });
  assert.equal((await readFrame(socket)).type, 'subscribed');
  handlers.get('message_start')({ type: 'message_start', message: { role: 'assistant', text: 'safe update' } });
   const event = await readFrame(socket);
   assert.equal(event.type, 'event');
   assert.equal(event.event.text, 'safe update');
   idle = false;
   sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-queued', idempotencyKey: 'input-2', operation: 'prompt', text: 'queued browser input', deliverAs: 'followUp' });
   const queuedReply = await readFrame(socket);
   assert.equal(queuedReply.type, 'accepted');
   assert.equal(queuedReply.deliveryState, 'queued');
   sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-queue-state', operation: 'state' });
   const queuedState = await readFrame(socket);
   assert.equal(queuedState.state.queued[0].id, 'input-2');
   sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-remove-queued', idempotencyKey: 'remove-1', operation: 'removeQueued', inputId: 'input-2' });
   const removed = await readFrame(socket);
   assert.equal(removed.type, 'accepted');
   assert.equal(removed.deliveryState, 'removed');
   assert.equal(queued.length, 0);
   sendFrame(socket, { protocolVersion: 2, type: 'command', requestId: 'web-3', idempotencyKey: 'input-3', operation: 'prompt', text: 'idle-only prompt' });
   const idleConflict = await readFrame(socket);
  assert.equal(idleConflict.type, 'rejected');
  assert.equal(idleConflict.error.code, 'CP_INPUT_CONFLICT');
  handlers.get('session_shutdown')();
  socket.destroy();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(fs.existsSync(descriptorPath), false, 'descriptor removed on shutdown');

  for (const [key, value] of Object.entries(previous)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  fs.rmSync(root, { recursive: true, force: true });
  console.log('web-session bridge contract: ok');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
