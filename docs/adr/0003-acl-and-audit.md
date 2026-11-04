# ADR 0003 — ACL at retrieval time, and an append-only audit log

## Status

Accepted.

## Decision

- The authorization predicate lives in one SQL expression and one Python function, and tests cover both. Post-filtering a global ranking is not the boundary.
- `private` is owner plus admin. Grants do not widen it. `restricted` adds user grants. `lab` is every authenticated user.
- Unauthorized reads of a real document are 404 and write `permission_denied`. Search simply returns no such chunk.
- Audit rows are written on a separate session so they survive a rollback of the business transaction.
- A trigger rejects UPDATE, and rejects DELETE unless the retention statement sets `labagent.audit_retention`.
- Detail keys that could carry a prompt, question, document body, email, or token are dropped. Client addresses are stored as an HMAC truncated with the JWT secret.

## Consequences

Operators can reconstruct who retrieved which chunk ids and which model was called. They cannot reconstruct the question from the audit log. Product transcripts live in `messages`, which is a different store and is visible only to that user.
