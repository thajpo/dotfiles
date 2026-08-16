'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { spawnSync } = require('node:child_process');

const repoRoot = path.resolve(__dirname, '../../../..');
const installed = path.join(
  os.homedir(),
  '.local/share/pi/core/node_modules/@earendil-works/pi-coding-agent',
);

function linkDependencies(sourceRoot, destinationRoot) {
  fs.mkdirSync(destinationRoot, { recursive: true });
  for (const entry of fs.readdirSync(sourceRoot, { withFileTypes: true })) {
    if (entry.name === '@earendil-works') continue;
    fs.symlinkSync(path.join(sourceRoot, entry.name), path.join(destinationRoot, entry.name));
  }
  const sourceScope = path.join(sourceRoot, '@earendil-works');
  const destinationScope = path.join(destinationRoot, '@earendil-works');
  fs.mkdirSync(destinationScope, { recursive: true });
  for (const entry of fs.readdirSync(sourceScope, { withFileTypes: true })) {
    if (entry.name === 'pi-coding-agent') continue;
    fs.symlinkSync(path.join(sourceScope, entry.name), path.join(destinationScope, entry.name));
  }
}

async function main() {
  if (!fs.existsSync(path.join(installed, 'dist/core/agent-session.js'))) {
    console.log('pinned Pi core overlay contract: skipped (core is not installed)');
    return;
  }

  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-web-core-'));
  try {
    const packageRoot = path.join(root, 'node_modules/@earendil-works/pi-coding-agent');
    fs.mkdirSync(packageRoot, { recursive: true });
    fs.cpSync(path.join(installed, 'dist'), path.join(packageRoot, 'dist'), { recursive: true });
    fs.copyFileSync(path.join(installed, 'package.json'), path.join(packageRoot, 'package.json'));
    linkDependencies(path.join(installed, 'node_modules'), path.join(root, 'node_modules'));

    for (const patchName of [
      'pi-coding-agent-0.83.0-web-dispatch.patch',
      'pi-coding-agent-0.83.0-web-queue-identity.patch',
    ]) {
      const result = spawnSync(
        'patch',
        ['--batch', '--forward', '--fuzz=0', '-p1', '-i', path.join(repoRoot, 'pi/patches', patchName)],
        { cwd: packageRoot, encoding: 'utf8' },
      );
      assert.equal(result.status, 0, result.stderr || result.stdout);
    }

    const { AgentSession } = await import(pathToFileURL(path.join(packageRoot, 'dist/core/agent-session.js')).href);
    const updates = [];
    const followUpMessages = [];
    const session = Object.create(AgentSession.prototype);
    session._eventListeners = [];
    session._emit = (event) => updates.push(event);
    session._steeringMessages = [];
    session._steeringQueueIds = [];
    session._followUpMessages = [];
    session._followUpQueueIds = [];
    session.agent = {
      steeringQueue: { messages: [] },
      followUpQueue: { messages: followUpMessages },
      steer(message) {
        this.steeringQueue.messages.push(message);
      },
      followUp(message) {
        followUpMessages.push(message);
      },
    };

    await session._queueFollowUp('same text', undefined, 'browser-a');
    await session._queueFollowUp('same text', undefined, 'browser-b');
    assert.deepEqual(session.getQueuedMessages(), [
      { id: 'browser-a', text: 'same text', deliverAs: 'followUp' },
      { id: 'browser-b', text: 'same text', deliverAs: 'followUp' },
    ]);
    assert.equal(session.agent.followUpQueue.messages[0].webInputId, 'browser-a');
    assert.equal(session.agent.followUpQueue.messages[1].webInputId, 'browser-b');

    assert.equal(session.removeQueuedMessage('browser-b'), true);
    assert.deepEqual(session.getQueuedMessages(), [
      { id: 'browser-a', text: 'same text', deliverAs: 'followUp' },
    ]);
    assert.equal(session.agent.followUpQueue.messages.length, 1);
    assert.equal(session.agent.followUpQueue.messages[0].webInputId, 'browser-a');
    assert.equal(session.removeQueuedMessage('browser-b'), false);
    assert.equal(updates.length, 3, 'two queue additions and one removal emit updates');
    console.log('pinned Pi core overlay contract: ok');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
