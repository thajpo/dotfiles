# Pi Greenfield Acceptance Contract

Owns: gate evaluation and release-verifier behavior. Program phase values and
the current phase table are owned only by the root program document.

A gate evaluation binds one source tree, one staged build when applicable, one
action catalog digest, one evidence-schema digest, exact commands, and retained
evidence. Results from incompatible identities cannot be combined.

Rules:

1. Source checks establish only document, schema, catalog, or source behavior.
2. Installed checks require an observed action through the exact staged Pi
   product path; lower-tier evidence cannot substitute.
3. Cumulative evaluation reruns or revalidates every dependency against
   compatible identities. It never trusts a prose claim.
4. STOP, SKIP, missing, stale, malformed, planned-only, mismatched, or
   unobservable evidence blocks the gate.
5. The release verifier rejects an action catalog containing a planned or
   excluded release action and rejects evidence whose action, scenario, or tier
   linkage is invalid.
6. The release verifier requires installed-product observation and explicit
   production-mutation and remote-provider observations for every release
   scenario.
7. Test success grants no activation authority. Final activation remains a
   separate human decision bound to one build and cutover plan.

The P0 evaluator is source-only and must not require future implementation,
Docker, launchers, tmux, installation, live state, network, or remote actions.
Later evaluators must use the exact entry and exit gates in the root program.
