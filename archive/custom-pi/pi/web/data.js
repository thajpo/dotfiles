(function () {
  'use strict';

  var data = {
    meta: {
      name: 'Pi Web',
      version: '0A fixture',
      source: 'slice-0a'
    },

    projects: [
      {
        id: 'pi-control-plane',
        name: 'Pi Control Plane',
        status: 'working',
        activitySummary: 'Workstream verifying the bridge; two decisions awaiting you.',
        lastUpdate: '4m ago',
        lastUpdateMin: 4,
        workingNow: [
          {
            id: 'run-pcp-workstream',
            title: 'Verifying the browser-bridge handshake',
            role: 'workstream',
            conversationId: 'cf-pcp-workstream',
            startedAgo: '4m'
          }
        ],
        changes: ['chg-2', 'chg-3'],
        conversations: [
          'cf-pcp-secretary',
          'cf-pcp-personal',
          'cf-pcp-workstream',
          'cf-pcp-reviewer',
          'cf-pcp-investigator'
        ],
        investigations: [
          {
            id: 'inv-pcp',
            title: 'Reading the runtime feasibility notes',
            role: 'investigator',
            conversationId: 'cf-pcp-investigator',
            state: 'working',
            age: '15m ago',
            ageMin: 15
          }
        ],
        recentOutcomes: [
          {
            id: 'out-pcp-schema',
            title: 'Settled the projection schema shape',
            role: 'secretary',
            state: 'completed',
            age: '40m ago',
            ageMin: 40
          }
        ]
      },

      {
        id: 'dotfiles',
        name: 'Dotfiles',
        status: 'idle',
        activitySummary: 'Secretary waiting on your confirmation; personal agent reworking the terminal startup layout.',
        lastUpdate: '12m ago',
        lastUpdateMin: 12,
        workingNow: [
          {
            id: 'run-dotfiles-personal',
            title: 'Reworking the terminal startup layout',
            role: 'personal',
            conversationId: 'cf-dotfiles-personal',
            startedAgo: '6m'
          }
        ],
        changes: ['chg-1'],
        conversations: ['cf-dotfiles-secretary', 'cf-dotfiles-personal'],
        investigations: [],
        recentOutcomes: [
          {
            id: 'out-dotfiles-recovery',
            title: 'Hardened recovery around the harness',
            role: 'personal',
            state: 'completed',
            age: '3h ago',
            ageMin: 180
          }
        ]
      },

      {
        id: 'vla-lens',
        name: 'VLA Lens',
        status: 'blocked',
        activitySummary: 'One failed run needs your choice.',
        lastUpdate: '20m ago',
        lastUpdateMin: 20,
        workingNow: [],
        changes: [],
        conversations: ['cf-vla-secretary', 'cf-vla-workstream'],
        investigations: [],
        recentOutcomes: [
          {
            id: 'out-vla-render',
            title: 'Render run failed',
            role: 'workstream',
            state: 'failed',
            age: '20m ago',
            ageMin: 20
          }
        ]
      },

      {
        id: 'personal-notes',
        name: 'Personal Notes',
        status: 'idle',
        activitySummary: 'No active work.',
        lastUpdate: '1d ago',
        lastUpdateMin: 1440,
        workingNow: [],
        changes: [],
        conversations: ['cf-notes-secretary'],
        investigations: [],
        recentOutcomes: []
      }
    ],

    conversations: [
      {
        id: 'cf-pcp-secretary',
        projectId: 'pi-control-plane',
        role: 'secretary',
        title: 'Project secretary',
        state: 'idle',
        lastUpdate: '9m ago',
        lastUpdateMin: 9,
        queued: [],
        timeline: [
          {
            kind: 'user',
            text: 'Summarize where the control plane work stands.',
            time: '50m ago'
          },
          { kind: 'tool', summary: 'Read 12 files', detail: 'Scanned the open changes and workstream records.', time: '50m ago' },
          {
            kind: 'assistant',
            markdown: 'Two changes are open. The bridge handshake is under review, and the projection schema is in revision. Two decisions are waiting for you in the inbox.',
            time: '46m ago'
          },
          { kind: 'message', text: 'Two decisions need you in the inbox.', time: '9m ago' }
        ]
      },

      {
        id: 'cf-pcp-personal',
        projectId: 'pi-control-plane',
        role: 'personal',
        title: 'Personal — projection schema',
        state: 'working',
        lastUpdate: '15m ago',
        lastUpdateMin: 15,
        queued: [],
        timeline: [
          {
            kind: 'user',
            text: 'Prepare the projection schema for review.',
            time: '1h ago'
          },
          { kind: 'tool', summary: 'Wrote 3 schema files', detail: 'Drafted the read-model schema files.', time: '1h ago' },
          {
            kind: 'assistant',
            markdown: 'The schema draft is ready. I am checking the field naming before submitting.',
            time: '55m ago'
          },
          { kind: 'tool', summary: 'Ran focused tests (12 passed)', time: '30m ago' },
          { kind: 'tool', summary: 'Started reviewer: API boundaries', time: '22m ago' },
          { kind: 'tool', summary: 'Submitted change revision 1', changeId: 'chg-3', time: '15m ago' }
        ]
      },

      {
        id: 'cf-pcp-workstream',
        projectId: 'pi-control-plane',
        role: 'workstream',
        title: 'Workstream — web preflight bridge',
        state: 'waiting',
        lastUpdate: '2m ago',
        lastUpdateMin: 2,
        queued: [],
        pinnedDecision: 'dec-workstream-1',
        timeline: [
          { kind: 'message', text: 'Workstream started: validate the browser-bridge runtime.', time: '40m ago' },
          { kind: 'tool', summary: 'Read 9 files', detail: 'Reviewed the runtime feasibility notes.', time: '38m ago' },
          {
            kind: 'tool',
            summary: 'Started a sandbox probe',
            detail: 'Focused feasibility probe in the assigned working copy.',
            time: '20m ago'
          },
          {
            kind: 'assistant',
            markdown: 'The handshake path looks viable. I want to confirm the exact runtime behavior before committing to the design.',
            time: '18m ago'
          },
          { kind: 'decision', decisionId: 'dec-workstream-1', time: '4m ago' },
          { kind: 'tool', summary: 'Tool completed', time: '2m ago' }
        ]
      },

      {
        id: 'cf-pcp-reviewer',
        projectId: 'pi-control-plane',
        role: 'reviewer',
        title: 'Reviewer — API boundaries',
        state: 'waiting',
        lastUpdate: '9m ago',
        lastUpdateMin: 9,
        queued: [],
        pinnedDecision: 'dec-review-1',
        timeline: [
          { kind: 'message', text: 'Review requested: API boundary check for the schema change.', time: '22m ago' },
          {
            kind: 'assistant',
            markdown: 'Scope: verify that the web projection keeps read boundaries and does not expose internal state. Ready for your confirmation.',
            time: '21m ago'
          },
          { kind: 'decision', decisionId: 'dec-review-1', time: '9m ago' }
        ]
      },

      {
        id: 'cf-pcp-investigator',
        projectId: 'pi-control-plane',
        role: 'investigator',
        title: 'Investigator — runtime feasibility',
        state: 'working',
        lastUpdate: '10m ago',
        lastUpdateMin: 10,
        queued: [],
        timeline: [
          { kind: 'message', text: 'Investigation started: reading the runtime feasibility notes.', time: '15m ago' },
          { kind: 'tool', summary: 'Read 5 files', detail: 'Read the runtime and restart behavior notes.', time: '14m ago' },
          {
            kind: 'assistant',
            markdown: 'Two open questions: exact input ordering and restart behavior. Working through both.',
            time: '10m ago'
          }
        ]
      },

      {
        id: 'cf-dotfiles-secretary',
        projectId: 'dotfiles',
        role: 'secretary',
        title: 'Project secretary',
        state: 'idle',
        lastUpdate: '10m ago',
        lastUpdateMin: 10,
        queued: [],
        timeline: [
          {
            kind: 'user',
            text: 'Please help me tidy the terminal startup layout.',
            time: '2h ago'
          },
          { kind: 'tool', summary: 'Read 4 files', detail: 'Reviewed the startup and shell config files.', time: '2h ago' },
          {
            kind: 'assistant',
            markdown: 'I found three files that control the layout. I can reorganize them into one clear startup file without changing behavior.',
            time: '2h ago'
          },
          {
            kind: 'user',
            text: 'Do it, but keep a way to restore the old behavior.',
            time: '2h ago'
          },
          { kind: 'tool', summary: 'Updated 3 files', detail: 'Consolidated the startup files and kept a restore note.', time: '2h ago' },
          { kind: 'message', text: 'Change "Rework the terminal startup layout" awaits your review.', changeId: 'chg-1', time: '12m ago' },
          {
            kind: 'user',
            text: 'Review it when you can and tell me the plan.',
            time: '10m ago'
          },
          {
            kind: 'assistant',
            markdown: 'The change is ready for review. It replaces the old startup steps with one clear file and keeps a documented way back.',
            time: '10m ago'
          },
          {
            kind: 'message',
            text: 'Needs you: confirm merge direction for the terminal startup layout.',
            attentionId: 'it-msg-merge',
            time: '12m ago'
          }
        ]
      },

      {
        id: 'cf-dotfiles-personal',
        projectId: 'dotfiles',
        role: 'personal',
        title: 'Personal — terminal layout',
        state: 'working',
        lastUpdate: '18m ago',
        lastUpdateMin: 18,
        queued: [
          { id: 'q-dot-1', preview: 'Also check the status line when you get a moment.' }
        ],
        timeline: [
          {
            kind: 'user',
            text: 'Start reworking the layout and keep the restore path.',
            time: '1h ago'
          },
          { kind: 'tool', summary: 'Read 6 files', detail: 'Read the startup, aliases, and prompt files.', time: '1h ago' },
          {
            kind: 'assistant',
            markdown: 'I have the full picture. Starting with the startup file, then the aliases.',
            time: '58m ago'
          },
          { kind: 'tool', summary: 'Updated 2 files', detail: 'Rewrote the startup file and aliases.', time: '40m ago' },
          { kind: 'tool', summary: 'Ran focused tests (18 passed)', time: '38m ago' },
          { kind: 'tool', summary: 'Started reviewer: API boundaries', time: '25m ago' },
          { kind: 'tool', summary: 'Submitted change revision 2', changeId: 'chg-1', time: '18m ago' }
        ]
      },

      {
        id: 'cf-vla-secretary',
        projectId: 'vla-lens',
        role: 'secretary',
        title: 'Project secretary',
        state: 'unavailable',
        lastUpdate: '20m ago',
        lastUpdateMin: 20,
        queued: [],
        timeline: [
          {
            kind: 'user',
            text: 'Is the lens render finished?',
            time: '30m ago'
          },
          { kind: 'tool', summary: 'Checked run outcome', detail: 'Run stopped before completion.', time: '25m ago' },
          {
            kind: 'assistant',
            markdown: 'The render run stopped mid-work. Nothing in the working copy was lost. Choose retry or investigate first.',
            time: '25m ago'
          },
          { kind: 'failure', text: 'Render run stopped before completion. Working copy is intact.', time: '20m ago' }
        ]
      },

      {
        id: 'cf-vla-workstream',
        projectId: 'vla-lens',
        role: 'workstream',
        title: 'Workstream — lens render',
        state: 'unavailable',
        lastUpdate: '20m ago',
        lastUpdateMin: 20,
        queued: [],
        timeline: [
          { kind: 'message', text: 'Workstream started: render the lens result.', time: '50m ago' },
          { kind: 'tool', summary: 'Read 3 files', detail: 'Read the lens config and render notes.', time: '49m ago' },
          { kind: 'tool', summary: 'Ran the render pipeline', detail: 'Executed the configured render steps.', time: '30m ago' },
          {
            kind: 'failure',
            text: 'Render run failed. Choose retry with extra logging or investigate first.',
            attentionId: 'it-msg-failed',
            time: '20m ago'
          }
        ]
      },

      {
        id: 'cf-notes-secretary',
        projectId: 'personal-notes',
        role: 'secretary',
        title: 'Project secretary',
        state: 'idle',
        lastUpdate: '1d ago',
        lastUpdateMin: 1440,
        queued: [],
        timeline: [
          {
            kind: 'user',
            text: 'Just noting this is my quiet project.',
            time: '1d ago'
          },
          {
            kind: 'assistant',
            markdown: 'Understood. I am here when you want to start something.',
            time: '1d ago'
          }
        ]
      }
    ],

    changes: [
      {
        id: 'chg-1',
        projectId: 'dotfiles',
        title: 'Rework the terminal startup layout',
        author: 'personal',
        state: 'pending_review',
        revisions: 2,
        summary: 'Consolidates three startup files into one clear file while keeping a documented restore path.',
        age: '18m ago',
        ageMin: 18,
        detail: {
          workingCopy: 'assigned copy for dotfiles',
          target: 'master',
          expected: 'The new layout replaces the old startup steps.',
          risk: 'Low. The restore path is documented and tested.',
          alreadyChanged: 'Two files rewritten and the focused tests updated.',
          preservedIfRejected: 'The old startup files remain committed and untouched.'
        }
      },
      {
        id: 'chg-2',
        projectId: 'pi-control-plane',
        title: 'Browser-bridge handshake contract',
        author: 'workstream',
        state: 'awaiting_integration',
        revisions: 1,
        summary: 'Defines the versioned handshake between the browser and the live conversation.',
        age: '30m ago',
        ageMin: 30,
        detail: {
          workingCopy: 'assigned copy for the workstream',
          target: 'master',
          expected: 'Integration records the reviewed contract in history.',
          risk: 'Moderate. Runtime feasibility is still being validated.',
          alreadyChanged: 'Handshake contract reviewed against the runtime notes.',
          preservedIfRejected: 'The reviewed contract stays in the working copy, unreferenced.'
        }
      },
      {
        id: 'chg-3',
        projectId: 'pi-control-plane',
        title: 'Web projection schema v1',
        author: 'personal',
        state: 'in_revision',
        revisions: 1,
        summary: 'Normalizes the read model for the control plane projections.',
        age: '15m ago',
        ageMin: 15,
        detail: {
          workingCopy: 'assigned copy for the schema work',
          target: 'master',
          expected: 'The projection schema replaces the ad-hoc display buckets.',
          risk: 'Low. Read-only model, no state changes.',
          alreadyChanged: 'Schema files drafted and unit-checked.',
          preservedIfRejected: 'Existing projections remain in use unchanged.'
        }
      }
    ],

    inbox: [
      {
        kind: 'decision',
        id: 'dec-workstream-1',
        projectId: 'pi-control-plane',
        conversationId: 'cf-pcp-workstream',
        decisionKind: 'workstream_proposal',
        title: 'Approve workstream "Web preflight bridge"?',
        preview: 'The workstream validates the browser-bridge runtime in its own assigned working copy.',
        consequence: 'The workstream will run a focused feasibility probe and may write to its assigned working copy. No other project is touched.',
        expiry: '45m remaining',
        expired: false,
        state: 'needs_decision',
        age: '4m ago',
        ageMin: 4,
        requiresPasskey: false,
        technical: [
          'Request: workstream proposal',
          'Assigned working copy: isolated from other work',
          'No host or network commands in this proposal'
        ]
      },
      {
        kind: 'decision',
        id: 'dec-review-1',
        projectId: 'pi-control-plane',
        conversationId: 'cf-pcp-reviewer',
        decisionKind: 'review_confirmation',
        title: 'Confirm reviewer for API boundary check',
        preview: 'The proposed reviewer will examine the schema change against the read boundaries.',
        consequence: 'The reviewer reads the assigned working copy and reports findings to the conversation. Nothing is changed by reviewing.',
        expiry: '2h remaining',
        expired: false,
        state: 'needs_decision',
        age: '9m ago',
        ageMin: 9,
        requiresPasskey: false,
        technical: [
          'Request: review confirmation',
          'Scope: web projection read boundaries',
          'Reading only; no mutation is performed'
        ]
      },
      {
        kind: 'decision',
        id: 'dec-pkg-1',
        projectId: 'pi-control-plane',
        conversationId: 'cf-pcp-secretary',
        decisionKind: 'package_approval',
        title: 'Install exact-pinned httpx 0.28.1 for the web gateway?',
        preview: 'The gateway server needs this async HTTP dependency.',
        consequence: 'The package is installed into the Pi install root with your host write access.',
        expiry: '15m remaining',
        expired: false,
        state: 'needs_decision',
        age: '7m ago',
        ageMin: 7,
        requiresPasskey: true,
        technical: [
          'Command scope: exact pinned version 0.28.1',
          'Install root: ~/.local/share/pi-system',
          'One-use approval with passkey step-up'
        ]
      },
      {
        kind: 'decision',
        id: 'dec-stale-1',
        projectId: 'pi-control-plane',
        conversationId: 'cf-pcp-workstream',
        decisionKind: 'integration',
        title: 'Integrate the bridge handshake contract?',
        preview: 'The reviewed change is ready to integrate.',
        consequence: 'Integration writes the change into the working copy history.',
        expiry: 'expired',
        expired: true,
        state: 'stale',
        staleReason: 'The change moved to a new revision after this request was created.',
        age: '3h ago',
        ageMin: 180,
        requiresPasskey: false,
        technical: [
          'Request: integration decision',
          'Status: request changed after creation',
          'The previous revision cannot be submitted'
        ]
      },
      {
        kind: 'message',
        id: 'it-msg-merge',
        projectId: 'dotfiles',
        conversationId: 'cf-dotfiles-secretary',
        title: 'Confirm merge direction for the terminal startup layout',
        preview: 'Replace or extend the current startup behavior before integration.',
        role: 'reviewer',
        state: 'needs_ack',
        age: '12m ago',
        ageMin: 12
      },
      {
        kind: 'message',
        id: 'it-msg-failed',
        projectId: 'vla-lens',
        conversationId: 'cf-vla-workstream',
        title: 'A render run failed — retry or investigate?',
        preview: 'Choose retry with extra logging or investigate the cause first.',
        role: 'workstream',
        state: 'needs_decision',
        age: '20m ago',
        ageMin: 20
      }
    ]
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = data;
  } else {
    window.PiData = data;
  }
})();
