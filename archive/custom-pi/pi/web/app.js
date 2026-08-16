(function () {
  'use strict';

  var data = window.PiData;
  var core = window.PiCore;

  var els = {
    list: document.getElementById('list-pane'),
    detail: document.getElementById('detail-pane'),
    sheet: document.getElementById('decision-sheet'),
    announcer: document.getElementById('announcer'),
    navInboxCount: document.getElementById('nav-inbox-count'),
    topbarNote: document.getElementById('topbar-note')
  };

  var state = {
    previousRoute: null,
    focusReturn: null,
    projectsFilter: '',
    sheetReturnHash: null,
    live: false,
    eventSource: null,
    streamUrl: null,
    lastEventId: null,
    timelinePending: null,
    runtime: null,
    runtimeConversationId: null,
    liveRefreshPending: false,
    liveRefreshQueued: false
  };

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      var map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
      return map[ch];
    });
  }

  function markdown(text) {
    var blocks = String(text || '').split(/\n{2,}/).map(function (block) {
      var escaped = esc(block).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>');
      return '<p>' + escaped + '</p>';
    });
    return blocks.join('');
  }

  function announce(message) {
    els.announcer.textContent = message;
  }

  function roleChip(role) {
    return '<span class="role-chip role-chip-role-' + esc(role) + '">' + esc(core.roleLabel(role)) + '</span>';
  }

  function stateBadge(stateKey) {
    return '<span class="badge badge-state-' + esc(stateKey) + '">' + esc(core.decisionStateLabel(stateKey)) + '</span>';
  }

  function stateBadgeGeneric(stateKey) {
    return '<span class="badge badge-state-' + esc(stateKey) + '">' + esc(core.humanState(stateKey)) + '</span>';
  }

  function attentionRow(item) {
    var target = item.kind === 'decision'
      ? '<button type="button" class="btn btn-sm" data-action="open-decision" data-decision="' + esc(item.id) + '">Review decision</button>'
      : '<a class="ghost-link" href="#/projects/' + esc(item.projectId) + '/conversations/' + esc(item.conversationId) + '">Open</a>';
    var badge = item.stale
      ? stateBadge('stale')
      : item.expired
        ? stateBadge('expired')
        : stateBadge(item.state);
    var role = item.roleLabel ? roleChip(item.role) : '';
    var meta = [];
    meta.push('<span class="tech">' + esc(item.projectName) + '</span>');
    if (item.conversationTitle) {
      meta.push('<span>' + esc(item.conversationTitle) + '</span>');
    }
    if (role) {
      meta.push(role);
    }
    meta.push(badge);
    meta.push('<span>' + esc(item.age) + '</span>');
    return (
      '<div class="row">' +
      '<div class="row-main">' +
      '<div class="row-title">' + esc(item.title) + '</div>' +
      '<div class="row-sub">' + esc(item.preview) + '</div>' +
      '<div class="row-meta">' + meta.join('') + '</div>' +
      '</div>' +
      '<div class="row-actions">' + target + '</div>' +
      '</div>'
    );
  }

  function decisionCard(decision) {
    var badge = decision.stale
      ? stateBadge('stale')
      : decision.expired
        ? stateBadge('expired')
        : stateBadge(decision.state);
    var meta = [];
    meta.push('<span class="tech">' + esc(decision.projectName) + '</span>');
    if (decision.conversationTitle) {
      meta.push('<span>' + esc(decision.conversationTitle) + '</span>');
    }
    if (decision.requiresPasskey) {
      meta.push('<span class="attention-flag">Passkey required</span>');
    }
    if (decision.expiry) {
      meta.push('<span>' + esc(decision.expiry) + '</span>');
    }
    return (
      '<div class="decision-inline">' +
      '<div class="row decision-row">' +
      '<div class="row-main">' +
      '<div class="row-title">' + esc(decision.title) + '</div>' +
      '<div class="row-sub">' + esc(decision.preview) + '</div>' +
      '<div class="row-meta">' + meta.join('') + badge + '</div>' +
      '</div>' +
      '<div class="row-actions">' +
      '<button type="button" class="btn btn-sm" data-action="open-decision" data-decision="' + esc(decision.id) + '">Review decision</button>' +
      '</div>' +
      '</div>' +
      '</div>'
    );
  }

  function timelineHtml(entries) {
    return entries.map(function (entry) {
      if (entry.kind === 'user') {
        return (
          '<div class="msg msg-user">' + esc(entry.text) + '<span class="msg-time">' + esc(entry.time) + '</span></div>'
        );
      }
      if (entry.kind === 'assistant') {
        return (
          '<div class="msg msg-assistant">' + markdown(entry.markdown) + '<span class="msg-time">' + esc(entry.time) + '</span></div>'
        );
      }
      if (entry.kind === 'tool') {
        var detail = entry.detail
          ? '<span class="tool-detail">' + esc(entry.detail) + '</span>'
          : '';
        return (
          '<div class="tool-entry"><span><span class="tech">' + esc(entry.summary) + '</span>' + detail + '</span><span class="msg-time">' + esc(entry.time) + '</span></div>'
        );
      }
      if (entry.kind === 'message') {
        var attention = entry.attention
          ? '<div class="row-meta"><a class="ghost-link" href="#/projects/' + esc(entry.attention.projectId) + '/conversations/' + esc(entry.attention.conversationId) + '">Needs you</a></div>'
          : '';
        return (
          '<div class="durable-entry"><span class="entry-label">Project message</span>' + esc(entry.text) + attention + '<span class="msg-time">' + esc(entry.time) + '</span></div>'
        );
      }
      if (entry.kind === 'decision') {
        var decision = entry.decision;
        if (!decision) {
          return '';
        }
        return '<div class="durable-entry"><span class="entry-label">Decision request</span><span class="msg-time">' + esc(entry.time) + '</span>' + decisionCard(decision) + '</div>';
      }
      if (entry.kind === 'change') {
        var change = entry.change;
        var changeLabel = change ? esc(change.title) + ' (' + esc(change.stateLabel) + ')' : 'Submitted a change revision';
        return (
          '<div class="durable-entry"><span class="entry-label">Change</span>' + changeLabel +
          '<span class="msg-time">' + esc(entry.time) + '</span></div>'
        );
      }
      if (entry.kind === 'failure') {
        var failureAttention = entry.attention
          ? '<div class="row-meta"><a class="ghost-link" href="#/projects/' + esc(entry.attention.projectId) + '/conversations/' + esc(entry.attention.conversationId) + '">Review the outcome</a></div>'
          : '';
        return (
          '<div class="durable-entry failure-entry"><span class="entry-label">Interrupted</span>' + esc(entry.text) + failureAttention + '<span class="msg-time">' + esc(entry.time) + '</span></div>'
        );
      }
      if (entry.kind === 'continuity') {
        return (
          '<div class="durable-entry"><span class="entry-label">Summary</span>' + esc(entry.text) + '<span class="msg-time">' + esc(entry.time) + '</span></div>'
        );
      }
      return '';
    }).join('');
  }

  function queuedHtml(queued) {
    if (!queued.length) {
      return '';
    }
    var items = queued.map(function (item) {
      var remove = item.removable === false || !item.id
        ? ''
        : '<button type="button" class="queued-remove" data-action="remove-queued" data-queued="' + esc(item.id) + '" aria-label="Remove queued message">\u00d7</button>';
      return (
        '<div class="queued-item"><span>' + esc(item.preview) + '</span>' +
        remove + '</div>'
      );
    }).join('');
    return '<div class="queued-list">' + items + '</div>';
  }

  function composerHtml(cs) {
    if (cs.state === 'unavailable') {
      return (
        '<div class="composer">' +
        '<div class="recovery-panel">' +
        '<p><strong>This conversation is not running.</strong> The timeline above remains readable.</p>' +
        '<details><summary class="btn btn-sm">Show recovery options</summary>' +
        '<p class="composer-note">The installed launcher can restart this conversation. Starting a stopped conversation from the browser arrives in a later release.</p>' +
        '</details>' +
        '</div>' +
        '</div>'
      );
    }

    var pinned = '';
    if (cs.pinnedDecision) {
      var decision = core.buildDecisionCard(data, cs.pinnedDecision);
      if (decision) {
        pinned = '<div class="pinned-decision">' + decisionCard(decision) + '</div>';
      }
    }

    var liveRuntime = state.live && state.runtimeConversationId === cs.id ? state.runtime : null;
    var queued = liveRuntime && Array.isArray(liveRuntime.queued) ? queuedHtml(liveRuntime.queued) : queuedHtml(cs.queued);
    var liveNote = state.live ? 'Live input is controller-owned. Browser and terminal share this conversation.' : 'Fixture: input is not delivered to a live agent.';

    if (cs.state === 'working') {
      return (
        '<div class="composer">' +
        queued +
        pinned +
        '<form class="composer-form" data-mode="steer">' +
        '<label class="visually-hidden" for="composer-input">Message</label>' +
        '<textarea id="composer-input" rows="1" placeholder="Guidance for the active work\u2026"></textarea>' +
        '<div class="composer-row">' +
        '<div class="mode-toggle" role="radiogroup" aria-label="When to deliver">' +
        '<label><input type="radio" name="mode" value="steer" checked>Steer now</label>' +
        '<label><input type="radio" name="mode" value="queue">After current work</label>' +
        '</div>' +
        '<button type="submit" class="btn btn-primary">Send</button>' +
        '<button type="button" class="btn btn-danger" data-action="stop-work">Stop</button>' +
        '</div>' +
        '</form>' +
         '<p class="composer-note">' + liveNote + '</p>' +
        '</div>'
      );
    }

    return (
      '<div class="composer">' +
      queued +
      pinned +
      '<form class="composer-form" data-mode="send">' +
      '<label class="visually-hidden" for="composer-input">Message</label>' +
      '<textarea id="composer-input" rows="1" placeholder="Message the conversation\u2026"></textarea>' +
      '<div class="composer-row">' +
      '<button type="submit" class="btn btn-primary">Send</button>' +
      '</div>' +
      '</form>' +
       '<p class="composer-note">' + liveNote + '</p>' +
      '</div>'
    );
  }

  function runtimeHtml(cs) {
    if (!state.live || state.runtimeConversationId !== cs.id) {
      return '';
    }
    var runtime = state.runtime;
    if (!runtime) {
      return '<section class="runtime-panel"><span class="tech">Loading runtime controls...</span></section>';
    }
    if (runtime.error) {
      return '<section class="runtime-panel"><span class="tech">Runtime controls are temporarily unavailable.</span></section>';
    }
    var modelOptions = (runtime.availableModels || []).map(function (model) {
      var value = model.provider + '/' + model.id;
      var selected = runtime.model && runtime.model.provider + '/' + runtime.model.id === value ? ' selected' : '';
      return '<option value="' + esc(value) + '"' + selected + '>' + esc(model.name || value) + '</option>';
    }).join('');
    var levels = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
    var thinkingOptions = levels.map(function (level) {
      return '<option value="' + level + '"' + (runtime.thinkingLevel === level ? ' selected' : '') + '>' + level + '</option>';
    }).join('');
    return '<section class="runtime-panel" aria-label="Conversation controls">' +
      '<span class="runtime-state">' + (runtime.idle ? 'Idle' : 'Working') + (runtime.pendingMessages ? ' / queued input' : '') + '</span>' +
      (modelOptions ? '<label>Model <select data-action="select-model" aria-label="Model">' + modelOptions + '</select></label>' : '') +
      (runtime.thinkingLevel ? '<label>Thinking <select data-action="select-thinking" aria-label="Thinking level">' + thinkingOptions + '</select></label>' : '') +
      '</section>';
  }

  function conversationDetailHtml(cs, timeline, projectName) {
    var backHref = '#/projects/' + esc(cs.projectId);
    return (
      '<div class="detail-header">' +
      '<a class="back-btn" href="' + backHref + '" aria-label="Back to project">\u2190</a>' +
      '<div>' +
      '<div class="breadcrumb">' + esc(projectName) + '</div>' +
      '<h2 tabindex="-1" data-focus-heading>' + roleChip(cs.role) + ' ' + esc(cs.title) + ' ' + stateBadgeGeneric(cs.state) + '</h2>' +
      '</div>' +
      '</div>' +
      runtimeHtml(cs) +
      '<div class="timeline">' + timelineHtml(timeline) + '</div>' +
      composerHtml(cs)
    );
  }

  function changeDetailHtml(change, project) {
    var backHref = '#/projects/' + esc(change.projectId);
    var revisionAuthor = change.author ? ' by ' + esc(core.roleLabel(change.author)) : '';
    var detail = change.detail || {};
    var rows = [
      ['Expected effect', detail.expected],
      ['Known risk', detail.risk],
      ['Already changed', detail.alreadyChanged],
      ['Preserved if rejected', detail.preservedIfRejected],
      ['Target', detail.target]
    ];
    var technical = rows.map(function (row) {
      if (!row[1]) {
        return '';
      }
      return '<div class="sheet-row"><div class="sheet-row-label">' + esc(row[0]) + '</div><div class="sheet-row-value">' + esc(row[1]) + '</div></div>';
    }).join('');
    return (
      '<div class="detail-header">' +
      '<a class="back-btn" href="' + backHref + '" aria-label="Back to project">\u2190</a>' +
      '<div>' +
      '<div class="breadcrumb">' + esc(project.name) + ' / Change</div>' +
      '<h2 tabindex="-1" data-focus-heading>' + esc(change.title) + '</h2>' +
      '</div>' +
      '</div>' +
      '<div class="timeline">' +
      '<div class="durable-entry"><span class="entry-label">Change ' + esc(change.stateLabel) + '</span>' + esc(change.summary) + '</div>' +
      technical +
       '<div class="sheet-row"><div class="sheet-row-label">Revision</div><div class="sheet-row-value">revision ' + esc(change.revisions) + revisionAuthor + '</div></div>' +
      '</div>'
    );
  }

  function homeHtml(summary) {
    var attentionSection;
    if (summary.hasAttention) {
      var rows = summary.needsAttention.map(attentionRow).join('');
      attentionSection =
        '<section class="section" aria-label="Needs attention">' +
        '<h2>Needs attention</h2><div class="list-group">' + rows + '</div></section>';
    } else if (state.live) {
      attentionSection =
        '<section class="section" aria-label="Needs attention">' +
        '<h2>Needs attention</h2>' +
        '<p class="section-empty">Nothing needs you right now.</p></section>';
    } else {
      attentionSection =
        '<section class="section" aria-label="Needs attention">' +
        '<h2>Needs attention</h2>' +
        '<p class="section-empty">Nothing needs you right now.</p></section>';
    }

    var working = summary.workingNow.map(function (run) {
      return (
        '<a class="row" href="#/projects/' + esc(run.projectId) + '/conversations/' + esc(run.conversationId) + '">' +
        '<div class="row-main">' +
        '<div class="row-title">' + esc(run.title) + '</div>' +
        '<div class="row-meta">' + roleChip(run.role) + '<span class="tech">' + esc(run.projectName) + '</span><span>' + esc(run.startedAgo) + ' in</span>' + stateBadgeGeneric('working') + '</div>' +
        '</div>' +
        '</a>'
      );
    }).join('') || '<p class="section-empty">No active work right now.</p>';

    var review = summary.awaitingReview.map(function (change) {
      return (
        '<a class="row" href="#/projects/' + esc(change.projectId) + '/changes/' + esc(change.id) + '">' +
        '<div class="row-main">' +
        '<div class="row-title">' + esc(change.title) + '</div>' +
        '<div class="row-meta"><span class="tech">' + esc(change.projectName) + '</span><span class="badge badge-state-' + esc(change.state) + '">' + esc(change.stateLabel) + '</span><span>' + esc(change.age) + '</span></div>' +
        '</div>' +
        '</a>'
      );
    }).join('') || '<p class="section-empty">No changes awaiting review or integration.</p>';

    var done = summary.completedRecently.map(function (outcome) {
      return (
        '<div class="row">' +
        '<div class="row-main">' +
        '<div class="row-title">' + esc(outcome.title) + '</div>' +
        '<div class="row-meta">' + roleChip(outcome.role) + '<span class="tech">' + esc(outcome.projectName) + '</span><span>' + esc(outcome.age) + '</span>' + stateBadgeGeneric('completed') + '</div>' +
        '</div>' +
        '</div>'
      );
    }).join('') || '<p class="section-empty">Nothing completed recently.</p>';

    return (
      '<div class="page-head">' +
      '<h1 tabindex="-1" data-focus-heading>Home</h1>' +
      '<p>Quiet summary across projects.</p>' +
      '</div>' +
      attentionSection +
      '<section class="section" aria-label="Working now"><h2>Working now</h2><div class="list-group">' + working + '</div></section>' +
      '<section class="section" aria-label="Changes awaiting review or integration"><h2>Awaiting review or integration</h2><div class="list-group">' + review + '</div></section>' +
      '<section class="section" aria-label="Completed recently"><h2>Completed recently</h2><div class="list-group">' + done + '</div></section>'
    );
  }

  function projectsHtml(list) {
    var filter = state.projectsFilter.toLowerCase();
    var visible = list.filter(function (project) {
      return project.name.toLowerCase().indexOf(filter) !== -1;
    });
    var cards = visible.map(function (project) {
      var meta = [];
      meta.push('<span class="tech">' + project.attentionCount + ' attention</span>');
      meta.push('<span class="tech">' + project.openChangeCount + ' open changes</span>');
      meta.push('<span>' + project.lastUpdate + '</span>');
      return (
        '<a class="project-card" href="#/projects/' + esc(project.id) + '">' +
        '<h2>' + esc(project.name) + '</h2>' +
        '<p>' + esc(project.activitySummary) + '</p>' +
        '<div class="row-meta">' + meta.join('') + stateBadgeGeneric(project.status) + '</div>' +
        '</a>'
      );
    }).join('') || '<p class="section-empty">No projects match "' + esc(state.projectsFilter) + '".</p>';

    return (
      '<div class="page-head">' +
      '<h1 tabindex="-1" data-focus-heading>Projects</h1>' +
      '<p>Registered projects with concise state.</p>' +
      '</div>' +
      '<label class="visually-hidden" for="project-search">Filter projects</label>' +
      '<input id="project-search" class="search" type="search" placeholder="Filter projects" value="' + esc(state.projectsFilter) + '" data-action="filter-projects">' +
      cards
    );
  }

  function projectHtml(workspace) {
    var head =
      '<div class="page-head">' +
      '<h1 tabindex="-1" data-focus-heading>' + esc(workspace.name) + '</h1>' +
      '<p>' + esc(workspace.activitySummary) + '</p>' +
      '<div class="row-meta">' + stateBadgeGeneric(workspace.status) + '<span>' + esc(workspace.lastUpdate) + '</span></div>' +
      '</div>';

    if (workspace.empty) {
      var offers = workspace.conversations
        .filter(function (conversation) {
          return conversation.role === 'secretary' || conversation.role === 'personal';
        })
        .map(function (conversation) {
          return (
            '<a class="row" href="#/projects/' + esc(workspace.id) + '/conversations/' + esc(conversation.id) + '">' +
            '<div class="row-main"><div class="row-title">' + esc(conversation.title) + '</div>' +
            '<div class="row-meta">' + roleChip(conversation.role) + stateBadgeGeneric(conversation.state) + '</div></div></a>'
          );
        }).join('');
      return (
        head +
        '<div class="empty-hero">' +
        '<h2>No active Pi work</h2>' +
        '<p>Start with a conversation, or leave this project quiet for now.</p>' +
        '</div>' +
        '<section class="section" aria-label="Available conversations"><h2>Conversations</h2><div class="list-group">' + offers + '</div></section>'
      );
    }

    var sections = workspace.sections.map(function (section) {
      var rows = section.items.map(function (item) {
        if (section.key === 'attention') {
          return attentionRow(item);
        }
        if (section.key === 'working') {
          return (
            '<a class="row" href="#/projects/' + esc(workspace.id) + '/conversations/' + esc(item.conversationId) + '">' +
            '<div class="row-main"><div class="row-title">' + esc(item.title) + '</div>' +
            '<div class="row-meta">' + roleChip(item.role) + '<span>' + esc(item.startedAgo) + ' in</span>' + stateBadgeGeneric('working') + '</div></div></a>'
          );
        }
        if (section.key === 'changes') {
          return (
            '<a class="row" href="#/projects/' + esc(workspace.id) + '/changes/' + esc(item.id) + '">' +
            '<div class="row-main"><div class="row-title">' + esc(item.title) + '</div>' +
            '<div class="row-sub">' + esc(item.summary) + '</div>' +
            '<div class="row-meta">' + roleChip(item.authorLabel) + '<span class="badge badge-state-' + esc(item.state) + '">' + esc(item.stateLabel) + '</span><span>revision ' + esc(item.revisions) + '</span><span>' + esc(item.age) + '</span></div></div></a>'
          );
        }
        if (section.key === 'conversations') {
          var queuedNote = item.queuedCount
            ? '<span class="attention-flag">' + item.queuedCount + ' queued</span>'
            : '';
          return (
            '<a class="row" href="#/projects/' + esc(workspace.id) + '/conversations/' + esc(item.id) + '">' +
            '<div class="row-main"><div class="row-title">' + roleChip(item.role) + ' ' + esc(item.title) + '</div>' +
            '<div class="row-meta">' + stateBadgeGeneric(item.state) + '<span>' + esc(item.lastUpdate) + '</span>' + queuedNote + '</div></div></a>'
          );
        }
        if (section.key === 'investigations') {
          return (
            '<a class="row" href="#/projects/' + esc(workspace.id) + '/conversations/' + esc(item.conversationId) + '">' +
            '<div class="row-main"><div class="row-title">' + esc(item.title) + '</div>' +
            '<div class="row-meta">' + roleChip(item.role) + stateBadgeGeneric(item.state) + '<span>' + esc(item.age) + '</span></div></div></a>'
          );
        }
        if (section.key === 'outcomes') {
          return (
            '<div class="row"><div class="row-main"><div class="row-title">' + esc(item.title) + '</div>' +
            '<div class="row-meta">' + roleChip(item.role) + '<span>' + esc(item.age) + '</span>' + stateBadgeGeneric(item.state) + '</div></div></div>'
          );
        }
        return '';
      }).join('');
      return '<section class="section" aria-label="' + esc(section.heading) + '"><h2>' + esc(section.heading) + '</h2><div class="list-group">' + rows + '</div></section>';
    }).join('');

    return head + sections;
  }

  function inboxHtml(items) {
    var rows = items.map(attentionRow).join('');
    return (
      '<div class="page-head">' +
      '<h1 tabindex="-1" data-focus-heading>Inbox</h1>' +
      '<p>Only items that need your decision or acknowledgement.</p>' +
      '</div>' +
      '<div class="list-group">' + rows + '</div>'
    );
  }

  function renderList(route) {
    var html;
    if (route.name === 'projects') {
      html = projectsHtml(core.buildProjectList(data));
    } else if (route.name === 'project') {
      var workspace = core.buildProjectWorkspace(data, route.projectId);
      html = workspace ? projectHtml(workspace) : missingProject();
    } else if (route.name === 'inbox') {
      html = inboxHtml(core.buildInbox(data));
    } else if (route.name === 'conversation' || route.name === 'change' || route.name === 'decision') {
      var parent = route.name === 'conversation' || route.name === 'change'
        ? core.buildProjectWorkspace(data, route.projectId)
        : null;
      html = parent ? projectHtml(parent) : (route.name === 'decision' ? inboxHtml(core.buildInbox(data)) : missingProject());
    } else {
      html = homeHtml(core.buildSummary(data));
    }
    els.list.innerHTML = html;
  }

  function missingProject() {
    return '<div class="page-head"><h1 tabindex="-1">Project not found</h1><p><a class="ghost-link" href="#/projects">Back to projects</a></p></div>';
  }

  function renderDetail(route) {
    if (route.name === 'conversation') {
      var conversation = core.conversationState(data, route.conversationId);
      if (!conversation) {
        els.detail.innerHTML = missingProject();
        return;
      }
      var project = data.projects.filter(function (item) {
        return item.id === conversation.projectId;
      })[0];
      var timeline = core.normalizeTimeline(data, route.conversationId);
      els.detail.innerHTML = conversationDetailHtml(conversation, timeline, project.name);
    } else if (route.name === 'change') {
      var change = data.changes.filter(function (item) {
        return item.id === route.changeId;
      })[0];
      var project2 = data.projects.filter(function (item) {
        return item.id === route.projectId;
      })[0];
      if (!change || !project2) {
        els.detail.innerHTML = missingProject();
        return;
      }
      els.detail.innerHTML = changeDetailHtml(change, project2);
    }
  }

  function renderNav(route) {
    var active = route.name === 'conversation' || route.name === 'change'
      ? 'project'
      : route.name;
    if (active === 'decision') {
      active = 'inbox';
    }
    var links = document.querySelectorAll('[data-nav]');
    links.forEach(function (link) {
      if (link.getAttribute('data-nav') === active) {
        link.setAttribute('aria-current', 'page');
      } else {
        link.removeAttribute('aria-current');
      }
    });
    var total = core.buildSummary(data).totalAttention;
    els.navInboxCount.textContent = total > 0 ? String(total) : '';
    els.navInboxCount.hidden = total === 0;
    document.title = titleForRoute(route);
    els.topbarNote.textContent = state.live
      ? (route.name === 'project' ? 'Project workspace · live' : 'Live controller data')
      : (route.name === 'project' ? 'Project workspace' : 'Fixture data');
  }

  function titleForRoute(route) {
    if (route.name === 'projects') {
      return 'Projects \u00b7 Pi Web';
    }
    if (route.name === 'project') {
      var workspace = core.buildProjectWorkspace(data, route.projectId);
      return (workspace ? workspace.name : 'Project') + ' \u00b7 Pi Web';
    }
    if (route.name === 'inbox') {
      return 'Inbox \u00b7 Pi Web';
    }
    if (route.name === 'conversation') {
      var conversation = core.conversationState(data, route.conversationId);
      return (conversation ? conversation.title : 'Conversation') + ' \u00b7 Pi Web';
    }
    if (route.name === 'change') {
      return 'Change \u00b7 Pi Web';
    }
    return 'Home \u00b7 Pi Web';
  }

  function focusVisiblePane(route) {
    var scope = route.name === 'conversation' || route.name === 'change' ? els.detail : els.list;
    var heading = scope.querySelector('[data-focus-heading]');
    if (heading) {
      heading.focus({ preventScroll: true });
    }
  }

  function announceFor(route) {
    if (route.name === 'home') {
      var summary = core.buildSummary(data);
      if (summary.hasAttention) {
        announce('Home. ' + summary.totalAttention + ' items need attention.');
      } else {
        announce('Home. Nothing needs attention. ' + summary.workingNow.length + ' items working now.');
      }
    } else if (route.name === 'projects') {
      announce('Projects. ' + data.projects.length + ' projects.');
    } else if (route.name === 'project') {
      var workspace = core.buildProjectWorkspace(data, route.projectId);
      if (workspace) {
        announce(workspace.name + '. ' + workspace.attentionCount + ' items need attention.');
      }
    } else if (route.name === 'inbox') {
      announce('Inbox. ' + core.buildInbox(data).length + ' items need your decision or acknowledgement.');
    } else if (route.name === 'conversation') {
      var conversation = core.conversationState(data, route.conversationId);
      if (conversation) {
        announce(conversation.title + '. State: ' + conversation.stateLabel + '.');
      }
    } else if (route.name === 'change') {
      announce('Change loaded.');
    }
  }

  function openDecisionSheet(decisionId, trigger) {
    var decision = core.buildDecisionCard(data, decisionId);
    if (!decision) {
      return;
    }
    state.focusReturn = trigger || null;
    if (els.sheet.open) {
      return;
    }
    renderSheet(decision);
    els.sheet.showModal();
    document.body.classList.add('sheet-open');
    announce('Reviewing decision: ' + decision.title);
  }

  function renderSheet(decision) {
    var metaRows = [];
    metaRows.push(['Requested action', decision.title]);
    metaRows.push(['Project', decision.projectName]);
    if (decision.conversationTitle) {
      metaRows.push(['Conversation', decision.conversationTitle]);
    }
    metaRows.push(['Consequence', decision.consequence]);
    metaRows.push(['Expiry', decision.expiry]);
    if (decision.requiresPasskey) {
      metaRows.push(['Authentication', 'Approving requires a passkey step-up']);
    }

    var rowsHtml = metaRows.map(function (row) {
      return '<div class="sheet-row"><div class="sheet-row-label">' + esc(row[0]) + '</div><div class="sheet-row-value">' + esc(row[1]) + '</div></div>';
    }).join('');

    var technical = decision.technical.length
      ? '<details class="tech-details"><summary>Bounded technical details</summary><ul>' + decision.technical.map(function (line) {
          return '<li class="tech">' + esc(line) + '</li>';
        }).join('') + '</ul></details>'
      : '';

    var actions;
    if (decision.stale) {
      actions =
        '<div class="sheet-row"><div class="sheet-row-label">Status</div><div class="sheet-row-value">' + esc(decision.staleReason || 'Request changed') + '</div></div>' +
        '<p class="composer-note">This request can no longer be submitted. Review the current change and decide against the latest revision.</p>' +
        '<div class="sheet-actions"><button type="button" class="btn" data-action="close-sheet">Back</button></div>';
    } else if (decision.expired) {
      actions =
        '<p class="composer-note">This decision expired. No action can be taken.</p>' +
        '<div class="sheet-actions"><button type="button" class="btn" data-action="close-sheet">Back</button></div>';
    } else if (state.live) {
      actions =
        '<p class="composer-note">This read-only release can inspect the request, but browser approvals arrive in the passkey-gated decisions slice.</p>' +
        '<div class="sheet-actions"><button type="button" class="btn" data-action="close-sheet">Back</button></div>';
    } else {
      actions =
        '<div class="sheet-actions">' +
        '<button type="button" class="btn btn-danger" data-action="reject" data-decision="' + esc(decision.id) + '">Reject</button>' +
        '<button type="button" class="btn btn-primary" data-action="approve" data-decision="' + esc(decision.id) + '">Approve</button>' +
        '</div>';
    }

    els.sheet.innerHTML =
      '<div class="sheet-inner">' +
      '<div class="detail-header">' +
      '<button type="button" class="back-btn" data-action="close-sheet" aria-label="Close decision review">\u00d7</button>' +
      '<div><div class="breadcrumb">Inbox</div><h2 id="sheet-title" tabindex="-1" data-focus-heading>Review decision</h2></div>' +
      '</div>' +
      '<div class="sheet-body">' +
      '<p id="sheet-desc">' + esc(decision.title) + '</p>' +
      '<div class="row-meta">' + stateBadge(decision.state) + '<span class="attention-flag">Review required before choosing</span></div>' +
      rowsHtml +
      technical +
      '<div id="sheet-actions">' + actions + '</div>' +
      '</div>' +
      '</div>';

    var heading = els.sheet.querySelector('[data-focus-heading]');
    if (heading) {
      heading.focus({ preventScroll: false });
    }
  }

  function closeDecisionSheet() {
    if (!els.sheet.open) {
      return;
    }
    els.sheet.close();
    document.body.classList.remove('sheet-open');
    if (location.hash.indexOf('/decisions/') !== -1) {
      history.replaceState(null, '', '#/inbox');
      render();
    } else {
      var trigger = state.focusReturn;
      if (trigger && typeof trigger.isConnected === 'boolean' && trigger.isConnected) {
        trigger.focus({ preventScroll: false });
      }
    }
    state.focusReturn = null;
  }

  function parseRoute(hash) {
    var clean = hash.replace(/^#\/?/, '');
    var parts = clean.split('/').filter(Boolean);
    var name = parts[0] || 'home';
    if (name === 'projects' && parts[1]) {
      if (parts[2] === 'conversations' && parts[3]) {
        return { name: 'conversation', projectId: parts[1], conversationId: parts[3] };
      }
      if (parts[2] === 'changes' && parts[3]) {
        return { name: 'change', projectId: parts[1], changeId: parts[3] };
      }
      return { name: 'project', projectId: parts[1] };
    }
    if (name === 'inbox' && parts[1] === 'decisions' && parts[2]) {
      return { name: 'decision', requestId: parts[2] };
    }
    if (name === 'inbox') {
      return { name: 'inbox' };
    }
    if (name === 'projects') {
      return { name: 'projects' };
    }
    return { name: 'home' };
  }

  function render() {
    var route = parseRoute(location.hash);
    var previous = state.previousRoute;
    var hasDetail = route.name === 'conversation' || route.name === 'change';

    document.body.classList.toggle('has-detail', hasDetail);
    renderNav(route);
    renderList(route);

    if (hasDetail) {
      renderDetail(route);
      els.detail.hidden = false;
    } else {
      els.detail.hidden = true;
    }

    if (route.name === 'decision') {
      var decision = core.buildDecisionCard(data, route.requestId);
      if (decision) {
        openDecisionSheet(route.requestId, null);
      }
      history.replaceState(null, '', '#/inbox');
    }

    if (!hasDetail) {
      var previousWasDetail = previous && (previous.name === 'conversation' || previous.name === 'change');
      if (previousWasDetail && state.focusReturn && state.focusReturn.isConnected) {
        state.focusReturn.focus({ preventScroll: false });
        state.focusReturn = null;
      } else {
        focusVisiblePane(route);
      }
    } else {
      focusVisiblePane(route);
    }

    announceFor(route);
    if (state.live && route.name === 'conversation') {
      loadConversationTimeline(route);
      loadLiveRuntime(route);
    }
    state.previousRoute = route;
  }

  function handleSubmit(form) {
    var textarea = form.querySelector('textarea');
    var text = textarea ? textarea.value.trim() : '';
    if (!text) {
      return;
    }
    var timeline = form.closest('.pane-detail') ? form.closest('.pane-detail').querySelector('.timeline') : null;
    var mode = form.getAttribute('data-mode');
    var route = parseRoute(location.hash);
    if (state.live && route.name === 'conversation') {
      if (form.getAttribute('data-submitting') === 'true') {
        return;
      }
      form.setAttribute('data-submitting', 'true');
      var submitButton = form.querySelector('button[type="submit"]');
      if (submitButton) {
        submitButton.disabled = true;
      }
      var previousKey = form.getAttribute('data-idempotency-key');
      var previousText = form.getAttribute('data-idempotency-text');
      var submissionKey = previousKey && previousText === text ? previousKey : newIdempotencyKey();
      form.setAttribute('data-idempotency-key', submissionKey);
      form.setAttribute('data-idempotency-text', text);
      var selectedLive = mode === 'steer' ? form.querySelector('input[name="mode"]:checked') : null;
      bridgeRequest(route, 'prompt', {
        text: text,
        idempotencyKey: submissionKey,
        deliverAs: selectedLive && selectedLive.value === 'queue' ? 'followUp' : (selectedLive ? 'steer' : undefined)
      }).then(function (value) {
        textarea.value = '';
        form.removeAttribute('data-idempotency-key');
        form.removeAttribute('data-idempotency-text');
        announce(value && value.deliveryState === 'queued'
          ? 'Message queued for after current work.'
          : 'Message accepted by the live conversation.');
        return refreshLiveData().catch(function () {
          announce('Message accepted. Live status refresh is temporarily unavailable.');
        });
      }).catch(function (error) {
        announce(error.code === 'CP_BRIDGE_STALE'
          ? 'The controller restarted. Restart this conversation before sending more guidance.'
          : error.code === 'CP_INPUT_CONFLICT'
            ? 'The conversation is working. Choose Steer now or After current work.'
          : error.code === 'CP_INPUT_REJECTED'
            ? 'Pi rejected the message before delivery: ' + error.message
          : error.code === 'CP_DELIVERY_UNCERTAIN'
              ? 'Delivery is uncertain. Check the conversation before resending.'
          : 'Message was not accepted: ' + error.message);
        if (error.code && error.code !== 'CP_DELIVERY_UNCERTAIN') {
          form.removeAttribute('data-idempotency-key');
          form.removeAttribute('data-idempotency-text');
        }
      }).then(function () {
        form.removeAttribute('data-submitting');
        if (submitButton) {
          submitButton.disabled = false;
        }
      });
      return;
    }
    if (mode === 'steer') {
      var selected = form.querySelector('input[name="mode"]:checked');
      if (selected && selected.value === 'queue') {
        var queuedList = form.closest('.composer').querySelector('.queued-list');
        var queuedHtml2 = '<div class="queued-list"><div class="queued-item"><span>' + esc(text) + '</span><button type="button" class="queued-remove" data-action="remove-queued" data-queued="fixture" aria-label="Remove queued message">\u00d7</button></div></div>';
        if (queuedList) {
          queuedList.insertAdjacentHTML('beforeend', '<div class="queued-item"><span>' + esc(text) + '</span><button type="button" class="queued-remove" data-action="remove-queued" data-queued="fixture" aria-label="Remove queued message">\u00d7</button></div>');
        } else {
          form.insertAdjacentHTML('beforebegin', queuedHtml2);
        }
        announce('Queued for after current work (fixture).');
      } else {
        if (timeline) {
          timeline.insertAdjacentHTML('beforeend', '<div class="msg msg-user">' + esc(text) + '<span class="msg-time">now</span></div>');
        }
        announce('Steering guidance sent (fixture).');
      }
    } else {
      if (timeline) {
        timeline.insertAdjacentHTML('beforeend', '<div class="msg msg-user">' + esc(text) + '<span class="msg-time">now</span></div>');
      }
      announce('Message sent (fixture).');
    }
    textarea.value = '';
  }

  document.addEventListener('click', function (event) {
    var sheet = els.sheet;

    if (sheet.open && !sheet.contains(event.target) && event.target.closest('a')) {
      var sheetDeepLink = event.target.closest('a').getAttribute('href');
      if (sheetDeepLink && sheetDeepLink.indexOf('#/') === 0) {
        closeDecisionSheet();
        return;
      }
    }

    var action = event.target.closest('[data-action]');
    if (!action) {
      return;
    }
    var kind = action.getAttribute('data-action');

    if (kind === 'open-decision') {
      event.preventDefault();
      var trigger = event.target.closest('button') || event.target;
      openDecisionSheet(action.getAttribute('data-decision'), trigger);
      return;
    }

    if (kind === 'close-sheet') {
      event.preventDefault();
      closeDecisionSheet();
      return;
    }

    if (kind === 'approve' || kind === 'reject') {
      event.preventDefault();
      if (state.live) {
        announce('Browser approvals are not enabled in the read-only release.');
        return;
      }
      var approve = kind === 'approve';
      action.disabled = true;
      var actions = document.getElementById('sheet-actions');
      var note = approve
        ? 'Fixture: approval recorded locally. No controller was contacted.'
        : 'Fixture: rejection recorded locally. No controller was contacted.';
      actions.innerHTML = '<p class="composer-note">' + note + '</p>';
      announce(approve ? 'Decision approved (fixture).' : 'Decision rejected (fixture).');
      return;
    }

    if (kind === 'remove-queued') {
      var queueId = action.getAttribute('data-queued');
      var queueRoute = parseRoute(location.hash);
      if (state.live && queueRoute.name === 'conversation' && queueId && queueId !== 'fixture') {
        action.disabled = true;
        bridgeRequest(queueRoute, 'removeQueued', { inputId: queueId }).then(function () {
          announce('Queued message removed from the live conversation.');
          return loadLiveRuntime(queueRoute);
        }).catch(function (error) {
          announce(error.code === 'CP_QUEUE_ITEM_NOT_FOUND'
            ? 'That queued message is no longer removable.'
            : 'Queued message was not removed: ' + error.message);
          return loadLiveRuntime(queueRoute);
        }).then(function () {
          action.disabled = false;
        });
        return;
      }
      var item = action.closest('.queued-item');
      if (item) {
        item.remove();
        announce('Queued message removed (fixture).');
      }
      return;
    }

    if (kind === 'stop-work') {
      if (window.confirm(state.live ? 'Stop the active work? This will interrupt the live conversation.' : 'Stop the active work? This is a fixture demo.')) {
        var stopRoute = parseRoute(location.hash);
        if (state.live && stopRoute.name === 'conversation') {
          bridgeRequest(stopRoute, 'stop', {}).then(function () {
            announce('Stop accepted by the live conversation.');
            return refreshLiveData();
          }).catch(function (error) {
            announce(error.code === 'CP_BRIDGE_STALE'
              ? 'The controller restarted. Restart this conversation before stopping it.'
              : error.code === 'CP_DELIVERY_UNCERTAIN'
                ? 'The stop request may have been delivered. Check the conversation state before retrying.'
              : 'Stop was not accepted: ' + error.message);
          });
        } else {
          announce('Stop requested (fixture).');
        }
      }
      return;
    }

    if (kind === 'filter-projects') {
      return;
    }
  });

  document.addEventListener('input', function (event) {
    var target = event.target;
    if (target && target.getAttribute && target.getAttribute('data-action') === 'filter-projects') {
      state.projectsFilter = target.value;
      renderList(parseRoute(location.hash));
    }
  });

  document.addEventListener('change', function (event) {
    var target = event.target;
    if (!target || !target.getAttribute) {
      return;
    }
    var action = target.getAttribute('data-action');
    if (action !== 'select-model' && action !== 'select-thinking') {
      return;
    }
    var route = parseRoute(location.hash);
    if (!state.live || route.name !== 'conversation') {
      return;
    }
    target.disabled = true;
    var operation = action === 'select-model' ? 'setModel' : 'setThinking';
    var payload = action === 'select-model' ? { model: target.value } : { thinkingLevel: target.value };
    bridgeRequest(route, operation, payload).then(function () {
      announce(action === 'select-model' ? 'Model changed for this conversation.' : 'Thinking level changed.');
      return loadLiveRuntime(route);
    }).catch(function (error) {
      announce('Runtime setting was not changed: ' + error.message);
      return loadLiveRuntime(route);
    }).then(function () {
      target.disabled = false;
    });
  });

  document.addEventListener('submit', function (event) {
    var form = event.target.closest('.composer-form');
    if (form) {
      event.preventDefault();
      handleSubmit(form);
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && els.sheet.open) {
      closeDecisionSheet();
    }
  });

  function bridgeRequest(route, operation, payload) {
    var request = Object.assign({
      operation: operation,
      idempotencyKey: payload && payload.idempotencyKey ? payload.idempotencyKey : newIdempotencyKey()
    }, payload || {});
    return fetch('/api/v1/projects/' + encodeURIComponent(route.projectId) + '/conversations/' + encodeURIComponent(route.conversationId) + '/bridge', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (value) {
        if (!response.ok) {
          var error = new Error(value && value.error ? value.error.message : 'gateway rejected the request');
          error.code = value && value.error ? value.error.code : '';
          throw error;
        }
        if (value && (value.type === 'uncertain' || value.type === 'pending')) {
          var deliveryError = new Error(value.error && value.error.message ? value.error.message : 'delivery state is not yet proven');
          deliveryError.code = value.error && value.error.code ? value.error.code : 'CP_DELIVERY_UNCERTAIN';
          throw deliveryError;
        }
        return value;
      });
    });
  }

  function newIdempotencyKey() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return 'web-' + window.crypto.randomUUID();
    }
    return 'web-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
  }

  function refreshLiveData() {
    return fetch('/api/v1/bootstrap', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (response) {
        if (!response.ok) throw new Error('gateway returned ' + response.status);
        return response.json();
      })
      .then(function (payload) {
        if (!payload || !payload.data || !Array.isArray(payload.data.projects)) throw new Error('gateway payload is invalid');
        data = payload.data;
        render();
        return data;
      });
  }

  function loadConversationTimeline(route) {
    var key = route.projectId + '/' + route.conversationId;
    if (state.timelinePending === key) return;
    state.timelinePending = key;
    fetch('/api/v1/projects/' + encodeURIComponent(route.projectId) + '/conversations/' + encodeURIComponent(route.conversationId) + '/timeline', {
      credentials: 'same-origin',
      cache: 'no-store'
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok) {
          var error = new Error(payload && payload.error ? payload.error.message : 'timeline returned ' + response.status);
          error.code = payload && payload.error ? payload.error.code : '';
          throw error;
        }
        return payload;
      });
    }).then(function (payload) {
      var conversation = data.conversations.filter(function (item) { return item.id === route.conversationId; })[0];
      if (!conversation || !payload || !Array.isArray(payload.timeline)) return;
      conversation.timeline = payload.timeline;
      if (parseRoute(location.hash).name === 'conversation' && parseRoute(location.hash).conversationId === route.conversationId) {
        renderDetail(route);
      }
    }).catch(function (error) {
      announce(error.code === 'CP_BRIDGE_STALE'
        ? 'The controller restarted. Restart this conversation before reconnecting.'
        : 'Conversation loaded, but its timeline is temporarily unavailable.');
    }).then(function () {
      state.timelinePending = null;
    });
  }

  function loadLiveRuntime(route) {
    if (!state.live || route.name !== 'conversation') {
      return Promise.resolve();
    }
    return fetch('/api/v1/projects/' + encodeURIComponent(route.projectId) + '/conversations/' + encodeURIComponent(route.conversationId) + '/bridge/state', {
      credentials: 'same-origin',
      cache: 'no-store'
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok) {
          var error = new Error(payload && payload.error ? payload.error.message : 'runtime state returned ' + response.status);
          error.code = payload && payload.error ? payload.error.code : '';
          throw error;
        }
        return payload;
      });
    }).then(function (payload) {
      if (parseRoute(location.hash).name !== 'conversation' || parseRoute(location.hash).conversationId !== route.conversationId) {
        return;
      }
      state.runtime = payload;
      state.runtimeConversationId = route.conversationId;
      renderDetail(route);
    }).catch(function () {
      state.runtime = { error: true };
      state.runtimeConversationId = route.conversationId;
      renderDetail(route);
    });
  }

  function refreshLiveConversation(route) {
    if (state.liveRefreshPending) {
      state.liveRefreshQueued = true;
      return;
    }
    state.liveRefreshPending = true;
    refreshLiveData().then(function () {
      loadConversationTimeline(route);
    }).catch(function () {
      announce('Live update received, but the timeline could not refresh.');
    }).then(function () {
      state.liveRefreshPending = false;
      if (state.liveRefreshQueued) {
        state.liveRefreshQueued = false;
        refreshLiveConversation(parseRoute(location.hash));
      }
    });
  }

  function syncLiveStream(route) {
    if (state.eventSource && (route.name !== 'conversation' || state.streamUrl !== location.hash)) {
      state.eventSource.close();
      state.eventSource = null;
      state.streamUrl = null;
      state.lastEventId = null;
    }
    if (!state.live || route.name !== 'conversation' || state.eventSource) return;
    var url = '/api/v1/projects/' + encodeURIComponent(route.projectId) + '/conversations/' + encodeURIComponent(route.conversationId) + '/bridge/stream';
    if (state.lastEventId) {
      url += '?afterEventId=' + encodeURIComponent(state.lastEventId);
    }
    state.streamUrl = location.hash;
    state.eventSource = new EventSource(url, { withCredentials: true });
    state.eventSource.onmessage = function (event) {
      if (event.lastEventId) {
        state.lastEventId = event.lastEventId;
      }
      var payload;
      try { payload = JSON.parse(event.data); } catch { payload = null; }
      if (payload && payload.type === 'bridge_stale') {
        state.eventSource.close();
        state.eventSource = null;
        state.streamUrl = null;
        state.lastEventId = null;
        announce('The controller restarted. Restart this conversation before reconnecting.');
        return;
      }
      if (payload && payload.type === 'stream_end') {
        state.eventSource.close();
        state.eventSource = null;
        state.streamUrl = null;
        window.setTimeout(function () { syncLiveStream(parseRoute(location.hash)); }, 0);
        return;
      }
      refreshLiveConversation(parseRoute(location.hash));
    };
    state.eventSource.onerror = function () {
      announce('Live conversation connection interrupted. Retrying.');
    };
  }

  window.addEventListener('hashchange', function () {
    render();
    syncLiveStream(parseRoute(location.hash));
  });

  function loadLiveData() {
    if (location.protocol === 'file:' || !window.fetch) {
      return;
    }
    fetch('/api/v1/bootstrap', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('gateway returned ' + response.status);
        }
        return response.json();
      })
      .then(function (payload) {
        if (!payload || !payload.data || !Array.isArray(payload.data.projects)) {
          throw new Error('gateway payload is invalid');
        }
        data = payload.data;
        state.live = true;
        els.topbarNote.textContent = 'Live controller data';
        render();
        syncLiveStream(parseRoute(location.hash));
      })
      .catch(function () {
        els.topbarNote.textContent = 'Fixture data · gateway unavailable';
      });
  }

  var initial = parseRoute(location.hash);
  if (initial.name === 'home') {
    history.replaceState(null, '', '#/home');
  }
  render();
  loadLiveData();
})();
