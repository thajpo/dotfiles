(function (global) {
  'use strict';

  var ROLE_LABELS = {
    secretary: 'Secretary',
    personal: 'Personal',
    workstream: 'Workstream',
    reviewer: 'Reviewer',
    investigator: 'Investigator'
  };

  var STATE_LABELS = {
    idle: 'Idle',
    working: 'Working',
    waiting: 'Waiting for you',
    interrupted: 'Interrupted',
    unavailable: 'Not running'
  };

  var CHANGE_STATE_LABELS = {
    pending_review: 'Awaiting review',
    in_revision: 'In revision',
    awaiting_integration: 'Awaiting integration',
    integrated: 'Integrated'
  };

  var DECISION_STATE_LABELS = {
    needs_decision: 'Needs your decision',
    needs_ack: 'Needs your acknowledgement',
    stale: 'Request changed',
    expired: 'Expired'
  };

  var TIMELINE_KINDS = ['user', 'assistant', 'tool', 'message', 'decision', 'change', 'failure', 'continuity'];

  function humanState(state) {
    if (Object.prototype.hasOwnProperty.call(STATE_LABELS, state)) {
      return STATE_LABELS[state];
    }
    return String(state).replace(/_/g, ' ');
  }

  function changeStateLabel(state) {
    if (Object.prototype.hasOwnProperty.call(CHANGE_STATE_LABELS, state)) {
      return CHANGE_STATE_LABELS[state];
    }
    return String(state).replace(/_/g, ' ');
  }

  function decisionStateLabel(state) {
    if (Object.prototype.hasOwnProperty.call(DECISION_STATE_LABELS, state)) {
      return DECISION_STATE_LABELS[state];
    }
    return String(state).replace(/_/g, ' ');
  }

  function roleLabel(role) {
    return ROLE_LABELS[role] || String(role).replace(/_/g, ' ');
  }

  function indexBy(list) {
    var map = {};
    list.forEach(function (item) {
      map[item.id] = item;
    });
    return map;
  }

  function buildLookups(data) {
    return {
      projects: indexBy(data.projects),
      conversations: indexBy(data.conversations),
      changes: indexBy(data.changes),
      decisions: indexBy(data.inbox.filter(function (item) {
        return item.kind === 'decision';
      }))
    };
  }

  function isOpenChange(change) {
    return change.state === 'pending_review' || change.state === 'in_revision' || change.state === 'awaiting_integration';
  }

  function attentionModel(data, lookups, item) {
    var conv = item.conversationId ? lookups.conversations[item.conversationId] : null;
    var project = lookups.projects[item.projectId] || { name: 'Unknown project' };
    var role = item.role || (conv && conv.role) || null;
    return {
      id: item.id,
      kind: item.kind,
      decisionKind: item.decisionKind || null,
      projectId: item.projectId,
      projectName: project.name,
      conversationId: item.conversationId || null,
      conversationTitle: conv ? conv.title : null,
      role: role,
      roleLabel: role ? roleLabel(role) : null,
      title: item.title,
      preview: item.preview,
      state: item.state,
      stateLabel: decisionStateLabel(item.state),
      age: item.age,
      ageMin: item.ageMin,
      requiresPasskey: !!item.requiresPasskey,
      expired: !!item.expired,
      stale: item.state === 'stale'
    };
  }

  function buildSummary(data) {
    var lookups = buildLookups(data);

    var needsAttention = data.inbox
      .slice()
      .map(function (item) {
        return attentionModel(data, lookups, item);
      })
      .sort(function (a, b) {
        return a.ageMin - b.ageMin;
      });

    var workingNow = [];
    data.projects.forEach(function (project) {
      project.workingNow.forEach(function (run) {
        workingNow.push({
          projectId: project.id,
          projectName: project.name,
          title: run.title,
          role: run.role,
          roleLabel: roleLabel(run.role),
          conversationId: run.conversationId,
          state: 'working',
          startedAgo: run.startedAgo
        });
      });
    });

    var awaitingReview = data.changes
      .filter(function (change) {
        return change.state === 'pending_review' || change.state === 'awaiting_integration';
      })
      .map(function (change) {
        return {
          id: change.id,
          projectId: change.projectId,
          projectName: lookups.projects[change.projectId].name,
          title: change.title,
          state: change.state,
          stateLabel: changeStateLabel(change.state),
          revisions: change.revisions,
          age: change.age
        };
      })
      .sort(function (a, b) {
        return a.revisions - b.revisions;
      });

    var completedRecently = [];
    data.projects.forEach(function (project) {
      project.recentOutcomes.forEach(function (outcome) {
        if (outcome.state !== 'completed') {
          return;
        }
        completedRecently.push({
          projectId: project.id,
          projectName: project.name,
          title: outcome.title,
          role: outcome.role,
          roleLabel: roleLabel(outcome.role),
          state: outcome.state,
          age: outcome.age,
          ageMin: outcome.ageMin
        });
      });
    });
    completedRecently.sort(function (a, b) {
      return a.ageMin - b.ageMin;
    });

    return {
      hasAttention: needsAttention.length > 0,
      totalAttention: needsAttention.length,
      needsAttention: needsAttention,
      workingNow: workingNow,
      awaitingReview: awaitingReview,
      completedRecently: completedRecently
    };
  }

  function buildProjectList(data) {
    var lookups = buildLookups(data);
    return data.projects
      .map(function (project) {
        var inboxCount = data.inbox.filter(function (item) {
          return item.projectId === project.id;
        }).length;
        var openChanges = data.changes.filter(function (change) {
          return change.projectId === project.id && isOpenChange(change);
        }).length;
        return {
          id: project.id,
          name: project.name,
          status: project.status,
          statusLabel: humanState(project.status),
          activitySummary: project.activitySummary,
          attentionCount: inboxCount,
          openChangeCount: openChanges,
          lastUpdate: project.lastUpdate,
          lastUpdateMin: project.lastUpdateMin
        };
      })
      .sort(function (a, b) {
        if (b.attentionCount !== a.attentionCount) {
          return b.attentionCount - a.attentionCount;
        }
        return a.lastUpdateMin - b.lastUpdateMin;
      });
  }

  function buildProjectWorkspace(data, projectId) {
    var lookups = buildLookups(data);
    var project = lookups.projects[projectId];
    if (!project) {
      return null;
    }

    var attention = data.inbox
      .filter(function (item) {
        return item.projectId === projectId;
      })
      .slice()
      .sort(function (a, b) {
        return a.ageMin - b.ageMin;
      })
      .map(function (item) {
        return attentionModel(data, lookups, item);
      });

    var changes = project.changes
      .map(function (changeId) {
        return lookups.changes[changeId];
      })
      .filter(Boolean)
      .map(function (change) {
        return {
          id: change.id,
          title: change.title,
          author: change.author,
          authorLabel: roleLabel(change.author),
          state: change.state,
          stateLabel: changeStateLabel(change.state),
          revisions: change.revisions,
          summary: change.summary,
          age: change.age
        };
      });

    var conversations = project.conversations
      .map(function (conversationId) {
        return lookups.conversations[conversationId];
      })
      .filter(Boolean)
      .map(function (conversation) {
        return {
          id: conversation.id,
          role: conversation.role,
          roleLabel: roleLabel(conversation.role),
          title: conversation.title,
          state: conversation.state,
          stateLabel: humanState(conversation.state),
          lastUpdate: conversation.lastUpdate,
          lastUpdateMin: conversation.lastUpdateMin,
          queuedCount: conversation.queued ? conversation.queued.length : 0
        };
      })
      .sort(function (a, b) {
        return a.lastUpdateMin - b.lastUpdateMin;
      });

    var investigations = (project.investigations || []).map(function (inv) {
      return {
        id: inv.id,
        title: inv.title,
        role: inv.role,
        roleLabel: roleLabel(inv.role),
        conversationId: inv.conversationId,
        state: inv.state,
        age: inv.age
      };
    });

    var outcomes = (project.recentOutcomes || []).slice().sort(function (a, b) {
      return a.ageMin - b.ageMin;
    });

    var sections = [];
    if (attention.length) {
      sections.push({ key: 'attention', heading: 'Needs attention', items: attention });
    }
    if (project.workingNow.length) {
      sections.push({ key: 'working', heading: 'Working now', items: project.workingNow });
    }
    if (changes.length) {
      sections.push({ key: 'changes', heading: 'Changes', items: changes });
    }
    if (conversations.length) {
      sections.push({ key: 'conversations', heading: 'Conversations', items: conversations });
    }
    if (investigations.length) {
      sections.push({ key: 'investigations', heading: 'Investigations and reviewers', items: investigations });
    }
    if (outcomes.length) {
      sections.push({ key: 'outcomes', heading: 'Recent outcomes', items: outcomes });
    }

    return {
      id: project.id,
      name: project.name,
      status: project.status,
      statusLabel: humanState(project.status),
      activitySummary: project.activitySummary,
      lastUpdate: project.lastUpdate,
      empty: attention.length === 0 && project.workingNow.length === 0 && changes.length === 0 && investigations.length === 0 && outcomes.length === 0,
      attentionCount: attention.length,
      openChangeCount: changes.filter(isOpenChange).length,
      sections: sections,
      conversations: conversations,
      recentOutcomes: outcomes
    };
  }

  function normalizeToolSummary(entry) {
    if (entry.failed) {
      return 'Tool failed';
    }
    if (entry.summary && typeof entry.summary === 'string' && entry.summary.length) {
      return entry.summary;
    }
    return 'Tool completed';
  }

  function normalizeTimeline(data, conversationId) {
    var lookups = buildLookups(data);
    var conversation = lookups.conversations[conversationId];
    if (!conversation) {
      return null;
    }

    return conversation.timeline
      .filter(function (entry) {
        return !entry.hidden && TIMELINE_KINDS.indexOf(entry.kind) !== -1;
      })
      .map(function (entry) {
        var normalized = {
          kind: entry.kind,
          time: entry.time
        };
        if (entry.kind === 'user' || entry.kind === 'message' || entry.kind === 'failure') {
          normalized.text = entry.text;
        }
        if (entry.kind === 'assistant') {
          normalized.markdown = entry.markdown;
        }
        if (entry.kind === 'tool') {
          normalized.summary = normalizeToolSummary(entry);
          normalized.bounded = true;
          if (entry.detail) {
            normalized.detail = entry.detail;
          }
          if (entry.changeId) {
            normalized.changeId = entry.changeId;
          }
        }
        if (entry.kind === 'decision' && entry.decisionId) {
          var decision = lookups.decisions[entry.decisionId];
          normalized.decision = decision ? attentionModel(data, lookups, decision) : null;
          normalized.decisionId = entry.decisionId;
        }
        if (entry.kind === 'change' && entry.changeId) {
          var change = lookups.changes[entry.changeId];
          normalized.changeId = entry.changeId;
          normalized.change = change
            ? {
                id: change.id,
                title: change.title,
                state: change.state,
                stateLabel: changeStateLabel(change.state)
              }
            : null;
        }
        if (entry.attentionId) {
          var attention = data.inbox.filter(function (item) {
            return item.id === entry.attentionId;
          })[0];
          normalized.attention = attention ? attentionModel(data, lookups, attention) : null;
          normalized.attentionId = entry.attentionId;
        }
        if (entry.kind === 'continuity') {
          normalized.text = entry.text;
        }
        return normalized;
      });
  }

  function buildDecisionCard(data, decisionId) {
    var lookups = buildLookups(data);
    var decision = lookups.decisions[decisionId];
    if (!decision) {
      return null;
    }
    var model = attentionModel(data, lookups, decision);
    model.decisionKind = decision.decisionKind;
    model.consequence = decision.consequence;
    model.expiry = decision.expiry;
    model.expired = !!decision.expired;
    model.requiresPasskey = !!decision.requiresPasskey;
    model.technical = decision.technical || [];
    model.staleReason = decision.staleReason || null;
    model.canSubmit = decision.state === 'needs_decision' && !decision.expired;
    model.canAcknowledge = decision.state === 'needs_ack';
    model.collapsedApprove = false;
    return model;
  }

  function buildInbox(data) {
    var lookups = buildLookups(data);
    return data.inbox
      .slice()
      .map(function (item) {
        return attentionModel(data, lookups, item);
      })
      .sort(function (a, b) {
        var rank = { needs_decision: 0, needs_ack: 1, stale: 2, expired: 3 };
        var ar = Object.prototype.hasOwnProperty.call(rank, a.state) ? rank[a.state] : 4;
        var br = Object.prototype.hasOwnProperty.call(rank, b.state) ? rank[b.state] : 4;
        if (ar !== br) {
          return ar - br;
        }
        return a.ageMin - b.ageMin;
      });
  }

  function conversationState(data, conversationId) {
    var lookups = buildLookups(data);
    var conversation = lookups.conversations[conversationId];
    if (!conversation) {
      return null;
    }
    return {
      id: conversation.id,
      projectId: conversation.projectId,
      role: conversation.role,
      roleLabel: roleLabel(conversation.role),
      title: conversation.title,
      state: conversation.state,
      stateLabel: humanState(conversation.state),
      lastUpdate: conversation.lastUpdate,
      queued: conversation.queued || [],
      pinnedDecision: conversation.pinnedDecision || null
    };
  }

  var core = {
    ROLE_LABELS: ROLE_LABELS,
    STATE_LABELS: STATE_LABELS,
    TIMELINE_KINDS: TIMELINE_KINDS,
    humanState: humanState,
    changeStateLabel: changeStateLabel,
    decisionStateLabel: decisionStateLabel,
    roleLabel: roleLabel,
    buildLookups: buildLookups,
    buildSummary: buildSummary,
    buildProjectList: buildProjectList,
    buildProjectWorkspace: buildProjectWorkspace,
    normalizeTimeline: normalizeTimeline,
    buildDecisionCard: buildDecisionCard,
    buildInbox: buildInbox,
    conversationState: conversationState
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = core;
  } else {
    global.PiCore = core;
  }
})(typeof window !== 'undefined' ? window : globalThis);
