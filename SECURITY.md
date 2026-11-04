# Security

Lab Agent is a lab knowledge agent. The controls below are part of the product, not a checklist pasted on top.

## Reporting

Report a vulnerability privately to the repository owner. Do not open a public issue that includes exploit detail, tokens, or document text.

## Secrets

- Configuration comes from the environment. `JWT_SECRET` shorter than 32 characters, a placeholder, or a low-diversity string aborts startup.
- A non-local LLM provider other than Ollama refuses to start without `LLM_API_KEY`.
- `LLM_CAPTURE` is rejected unless `APP_ENV=test`. It exists so tests can prove redaction. It must stay off in any shared environment.
- Demo passwords in the seed script are labeled local fixtures (`LabAgent-Demo-2026-*`). `python -m app.seed` and the API entrypoint both refuse to load them when `APP_ENV=prod`. The login form prefills them only when `NEXT_PUBLIC_DEMO_HINTS=true`, which the Docker image leaves off unless a demo build opts in. Do not reuse these passwords.

## Access control

Retrieval SQL and direct document reads use the same rule: admins see everything, owners see their own documents, `lab` is visible to every signed-in user, `restricted` also allows explicit grants, and `private` ignores grants. A document the caller cannot read returns 404, including when it exists. Search omits those chunks instead of filtering them after ranking.

## Audit

`audit_events` is append-only. Updates are rejected. Deletes run only through the retention job, which sets a transaction-local flag the trigger recognizes. The log records action, actor id, resource id, request id, a hash of the client address, model name, and token counts. It does not record questions, answers, prompts, document text, or email addresses. Default retention is 180 days and can be set between 1 and 3650 days.

## Data sent to a model

Before any provider call, email addresses, mainland mobile numbers, and 18-digit resident identity numbers in the question and the cited chunks are replaced with tokens. The local extractive fallback still only quotes redacted evidence. Citations shown to an authorized user keep the original excerpt, because that user can already open the document.

## Grounding

The model is called only after a decision score clears a threshold that was fit on the development split. The score is cosine similarity, or the reranker score when a rerank mode is selected. Rank-fusion scores are not thresholds. If the check fails, the API returns a refusal and does not call the model.

## Dependencies and history

Continuous integration runs gitleaks on full history, `pip-audit`, and `npm audit --audit-level=high`, plus the test suite. The default embedder is a local hashing embedder so CI never downloads weights.
