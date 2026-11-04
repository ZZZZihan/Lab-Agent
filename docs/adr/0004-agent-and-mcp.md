# ADR 0004 — Agent loop and MCP tools

## Status

Accepted.

## Decision

- Tool choice is a deterministic policy inside a LangGraph loop: meeting words search meeting notes, project words search project notes, otherwise the knowledge base is searched. Paper words then call the paper-search port. A request for the full text fetches that document if a hit exists. The loop stops after four steps.
- The paper-search port returns `not_configured` and never invents a paper. Wiring a real provider is a later change behind the same interface.
- Project and meeting tools are knowledge-base searches filtered by collection. They are not empty stubs, because the lab notes already live in those collections.
- Generation runs only after the evidence decision. A refusal does not call the model.
- The same five tools are exposed on an MCP stdio server. The actor is `MCP_ACTOR_EMAIL`, loaded inside the request session, and the same ACL applies.
- The default model provider is the local extractive fallback so the vertical slice runs without an API key. OpenAI-compatible streaming is implemented for OpenAI, DeepSeek, Ollama, and vLLM.

## Consequences

Answers from the default provider quote a supporting sentence and name the section. They are not a fluent summary. Point `LLM_PROVIDER` at a real endpoint when that summary is worth the external call; the refusal gate stays in front of it.
