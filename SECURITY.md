# Security

Lab Agent is a lab knowledge agent. The controls below are part of the product, not a checklist pasted on top.

## Reporting

Report a vulnerability privately to the repository owner. Do not open a public issue that includes exploit detail, tokens, or document text.

## Secrets

- Configuration comes from the environment or a key file. `JWT_SECRET` shorter than 32 characters, a placeholder, or a low-diversity string aborts startup.
- A non-local LLM provider other than Ollama (`openai_compatible`, `openai`, `deepseek`, `vllm`) refuses to start without a resolved key from `LLM_API_KEY` or `LLM_API_KEY_FILE`.
- `LLM_CAPTURE` is rejected unless `APP_ENV=test`. It exists so tests can prove redaction. It must stay off in any shared environment.
- Demo passwords in the seed script are labeled local fixtures (`LabAgent-Demo-2026-*`). `python -m app.seed` and the API entrypoint both refuse to load them when `APP_ENV=prod`. The login form prefills them only when `NEXT_PUBLIC_DEMO_HINTS=true`, which the Docker image leaves off unless a demo build opts in. Do not reuse these passwords.

## Access control

Retrieval SQL and direct document reads use the same rule: admins see everything, owners see their own documents, `lab` is visible to every signed-in user, `restricted` also allows explicit grants, and `private` ignores grants. A document the caller cannot read returns 404, including when it exists. Search omits those chunks instead of filtering them after ranking.

## Audit

`audit_events` is append-only. Updates are rejected. Deletes run only through the retention job, which sets a transaction-local flag the trigger recognizes. The log records action, actor id, resource id, request id, a hash of the client address, model name, and token counts. It does not record questions, answers, prompts, document text, or email addresses. Default retention is 180 days and can be set between 1 and 3650 days.

## Data sent to a model

- LLM requests are sent to the local Magpie gateway (`LLM_PROVIDER=openai_compatible`), which forwards them to the upstream provider for the selected chat model.
- Only the user's question and the cited evidence chunks are sent. The lab has confirmed that outbound transmission of lab data to model providers is permitted. By default (`LLM_REDACT=true`), email addresses, mainland mobile numbers, and 18-digit resident identity numbers in the question and cited chunks are replaced with redaction tokens as defense in depth; this can be turned off with `LLM_REDACT=false` (recorded in the `llm_call` audit event with `"redact": false` and `redaction_count: 0`).
- Lab Agent uses a dedicated Magpie gateway key so its token usage and costs are tracked separately in Magpie.
- The gateway key is read from `LLM_API_KEY` or `LLM_API_KEY_FILE` (such as `~/.config/lab-agent/magpie.key` or `/run/secrets/magpie_key`) and is never written to the repository, `.env`, logs, audit events, or `/health` responses.
- Citations shown to an authorized user keep the original excerpt, because that user can already open the document.

## Grounding

The model is called only after a decision score clears a threshold that was fit on the development split. The score is cosine similarity, or the reranker score when a rerank mode is selected. Rank-fusion scores are not thresholds. If the check fails, the API returns a refusal and does not call the model.

## Dependencies and history

Continuous integration runs gitleaks on full history, `pip-audit`, and `npm audit --audit-level=high`, plus the test suite. The default embedder is a local hashing embedder so CI never downloads weights.
