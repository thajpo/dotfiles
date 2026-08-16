'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const data = require(path.join(__dirname, '..', 'data.js'));
const core = require(path.join(__dirname, '..', 'core.js'));

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('ok - ' + name);
  } catch (err) {
    failed += 1;
    console.error('FAIL - ' + name);
    console.error(String(err && err.message ? err.message : err).split('\n').map((l) => '    ' + l).join('\n'));
  }
}

const BANNED_JARGON = ['tmux', 'jsonl', 'worktree', 'epoch', 'socket', 'pane', 'session file', 'run id'];

function visibleText(value) {
  return String(value || '').toLowerCase();
}

function walkStrings(obj, out) {
  if (obj == null) {
    return;
  }
  if (typeof obj === 'string') {
    out.push(obj);
    return;
  }
  if (Array.isArray(obj)) {
    obj.forEach((item) => walkStrings(item, out));
    return;
  }
  if (typeof obj === 'object') {
    Object.keys(obj).forEach((key) => walkStrings(obj[key], out));
  }
}

test('fixture data loads', () => {
  assert.ok(Array.isArray(data.projects) && data.projects.length >= 3);
  assert.ok(Array.isArray(data.conversations) && data.conversations.length >= 5);
  assert.ok(Array.isArray(data.inbox) && data.inbox.length >= 4);
  assert.ok(Array.isArray(data.changes) && data.changes.length >= 1);
});

test('human state and role labels are plain language', () => {
  assert.equal(core.humanState('idle'), 'Idle');
  assert.equal(core.humanState('working'), 'Working');
  assert.equal(core.humanState('unavailable'), 'Not running');
  assert.equal(core.roleLabel('secretary'), 'Secretary');
  assert.equal(core.roleLabel('workstream'), 'Workstream');
  assert.equal(core.roleLabel('reviewer'), 'Reviewer');
});

test('inbox items resolve to existing projects and conversations', () => {
  const projects = new Set(data.projects.map((p) => p.id));
  const conversations = new Set(data.conversations.map((c) => c.id));
  data.inbox.forEach((item) => {
    assert.ok(projects.has(item.projectId), 'inbox ' + item.id + ' project resolves');
    if (item.conversationId) {
      assert.ok(conversations.has(item.conversationId), 'inbox ' + item.id + ' conversation resolves');
    }
  });
});

test('conversation references resolve to existing projects and roles are distinct', () => {
  const projects = new Set(data.projects.map((p) => p.id));
  const roles = new Set();
  data.conversations.forEach((c) => {
    assert.ok(projects.has(c.projectId), 'conversation ' + c.id + ' project resolves');
    roles.add(c.role);
  });
  assert.ok(roles.has('secretary'));
  assert.ok(roles.has('personal'));
  assert.ok(roles.has('workstream'));
});

test('project change ids resolve to existing changes', () => {
  const changes = new Set(data.changes.map((c) => c.id));
  data.projects.forEach((p) => {
    (p.changes || []).forEach((changeId) => {
      assert.ok(changes.has(changeId), p.id + ' change ' + changeId + ' resolves');
    });
  });
});

test('visible text avoids session and tmux jargon', () => {
  const strings = [];
  walkStrings(data, strings);
  strings.forEach((text) => {
    const lower = visibleText(text);
    BANNED_JARGON.forEach((token) => {
      assert.ok(lower.indexOf(token) === -1, 'found banned token "' + token + '" in "' + text + '"');
    });
  });
});

test('buildSummary orders needs-attention newest first and matches inbox', () => {
  const summary = core.buildSummary(data);
  assert.equal(summary.totalAttention, data.inbox.length);
  assert.ok(summary.hasAttention);
  assert.ok(summary.needsAttention.length >= 4);
  for (let i = 1; i < summary.needsAttention.length; i++) {
    assert.ok(
      summary.needsAttention[i - 1].ageMin <= summary.needsAttention[i].ageMin,
      'needs attention sorted by recency'
    );
  }
  assert.ok(summary.workingNow.length >= 2, 'working now has active projects');
  assert.ok(summary.awaitingReview.length >= 1, 'has changes awaiting review');
  assert.ok(summary.completedRecently.length >= 1, 'has completed outcomes');
});

test('buildProjectList sorts attention first then recent activity', () => {
  const list = core.buildProjectList(data);
  assert.equal(list[0].id, 'pi-control-plane');
  assert.equal(list[0].attentionCount, 4);
  assert.equal(list[0].openChangeCount, 2);
  const dotfiles = list.find((p) => p.id === 'dotfiles');
  assert.equal(dotfiles.attentionCount, 1);
  const quiet = list.find((p) => p.id === 'personal-notes');
  assert.equal(quiet.attentionCount, 0);
  for (let i = 1; i < list.length; i++) {
    const prev = list[i - 1];
    const cur = list[i];
    assert.ok(
      prev.attentionCount > cur.attentionCount ||
        (prev.attentionCount === cur.attentionCount && prev.lastUpdateMin <= cur.lastUpdateMin),
      'list sorted by attention then recency'
    );
  }
});

test('buildProjectWorkspace sections follow plan order and skip empty buckets', () => {
  const ws = core.buildProjectWorkspace(data, 'pi-control-plane');
  assert.equal(ws.name, 'Pi Control Plane');
  const keys = ws.sections.map((s) => s.key);
  assert.deepEqual(keys, ['attention', 'working', 'changes', 'conversations', 'investigations', 'outcomes']);
  assert.equal(ws.attentionCount, 4);
  assert.equal(ws.openChangeCount, 2);
});

test('buildProjectWorkspace shows empty state for quiet project', () => {
  const ws = core.buildProjectWorkspace(data, 'personal-notes');
  assert.ok(ws.empty, 'quiet project is empty');
  assert.equal(ws.attentionCount, 0);
  assert.ok(ws.conversations.some((c) => c.role === 'secretary'), 'offers secretary conversation');
});

test('normalizeTimeline keeps order, only safe kinds, and bounded tool summaries', () => {
  const timeline = core.normalizeTimeline(data, 'cf-dotfiles-personal');
  assert.ok(Array.isArray(timeline) && timeline.length >= 6);
  timeline.forEach((entry) => {
    assert.ok(core.TIMELINE_KINDS.indexOf(entry.kind) !== -1, 'known timeline kind');
    if (entry.kind === 'tool') {
      assert.ok(entry.bounded, 'tool entry is bounded');
      assert.ok(entry.summary && entry.summary.length, 'tool entry has human summary');
    }
  });
  const kinds = timeline.map((e) => e.kind);
  assert.ok(kinds.indexOf('user') !== -1);
  assert.ok(kinds.indexOf('assistant') !== -1);
  assert.ok(kinds.indexOf('tool') !== -1);
});

test('normalizeTimeline attaches inline decision cards', () => {
  const timeline = core.normalizeTimeline(data, 'cf-pcp-workstream');
  const decisionEntry = timeline.find((e) => e.kind === 'decision');
  assert.ok(decisionEntry, 'workstream timeline has decision entry');
  assert.ok(decisionEntry.decision, 'decision resolved to model');
  assert.equal(decisionEntry.decision.id, 'dec-workstream-1');
  assert.equal(decisionEntry.decision.projectName, 'Pi Control Plane');
});

test('normalizeTimeline renders unknown tool generically', () => {
  const summary = core.normalizeTimeline(data, 'cf-pcp-workstream').find((e) => e.kind === 'tool' && e.summary === 'Tool completed');
  assert.ok(summary, 'generic tool summary used');
  assert.equal(summary.summary, 'Tool completed');
});

test('decision card cannot approve from a collapsed row', () => {
  const card = core.buildDecisionCard(data, 'dec-workstream-1');
  assert.equal(card.collapsedApprove, false);
  assert.equal(card.canSubmit, true);
  assert.equal(card.kind, 'decision');
});

test('stale and expired decisions cannot be submitted', () => {
  const stale = core.buildDecisionCard(data, 'dec-stale-1');
  assert.equal(stale.stale, true);
  assert.equal(stale.canSubmit, false);
  assert.equal(stale.state, 'stale');
  const card = core.buildDecisionCard(data, 'dec-workstream-1');
  assert.equal(card.canSubmit, true);
});

test('buildInbox groups decisions and messages, resolves joins', () => {
  const inbox = core.buildInbox(data);
  assert.equal(inbox.length, data.inbox.length);
  const decision = inbox.find((i) => i.id === 'dec-pkg-1');
  assert.equal(decision.decisionKind, 'package_approval');
  assert.equal(decision.requiresPasskey, true);
  assert.equal(decision.projectName, 'Pi Control Plane');
  const message = inbox.find((i) => i.id === 'it-msg-merge');
  assert.equal(message.kind, 'message');
  assert.equal(message.roleLabel, 'Reviewer');
  assert.equal(message.conversationTitle, 'Project secretary');
});

test('conversationState exposes distinct roles and states for fixtures', () => {
  const states = {};
  data.conversations.forEach((c) => {
    states[c.role + ':' + c.state] = core.conversationState(data, c.id).stateLabel;
  });
  assert.ok(states['secretary:idle']);
  assert.ok(states['personal:working']);
  assert.ok(states['workstream:waiting']);
  assert.ok(states['secretary:unavailable']);
});

test('conversation queued items are preserved in state', () => {
  const cs = core.conversationState(data, 'cf-dotfiles-personal');
  assert.equal(cs.queued.length, 1);
  assert.equal(cs.queued[0].preview.length > 0, true);
});

console.log('');
console.log(passed + ' passed, ' + failed + ' failed');
if (failed > 0) {
  process.exit(1);
}
