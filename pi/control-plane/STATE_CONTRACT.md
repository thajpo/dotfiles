# Pi Greenfield State Contract

Owns: state identity, authority, freshness, and transitions.

Greenfield uses a new schema epoch under a new state root. It creates project,
working-copy, conversation, run, message, request, change, review, integration,
attention, and evidence identities only from explicit greenfield operations.
It never reads, imports, maps, resumes, reconciles, or adopts historical Pi
state or chats. Historical bytes remain untouched external data.

State authorities are disjoint:

| Data | Authority |
|---|---|
| Lifecycle records and resource versions | Greenfield controller SQLite store |
| Source content | Assigned working-copy files and controller-observed Git objects |
| Conversation history | Controller-selected Pi session JSONL |
| Active writer ownership | Database unique live-writer constraint, current claim/epoch, run identity, and kernel lifecycle lock together |
| Presentation | No state authority |

The canonical store and CLI are the greenfield families. Earlier store, schema,
client, CLI, runtime, workspace, registry, route-file, and chat-discovery
families are absent from release reachability.

Every mutation checks project identity, expected resource version, exact input
identity, and idempotency key. Reuse with different content fails. Message
delivery, acknowledgement, and resolution are distinct transitions. Unknown
liveness or partial effects become bounded attention records; they never
authorize deletion, adoption, or a replacement writer.

Writer acquisition takes the secure lifecycle lock before the SQLite claim,
then transactionally re-reads and compare-and-swap updates the working-copy
version, writer epoch, claim, and unique live run. A stale read cannot overwrite
a newer claim. Idempotent replay must reacquire and retain the same kernel lock;
the database, rather than the kernel lock alone, remains durable authority.

Normal schema upgrades may evolve this fresh epoch after release. They may not
be used as an import path from an earlier product.
