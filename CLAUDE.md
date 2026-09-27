# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project goal

Findr is a unified search platform: users connect data sources (Gmail, Slack, WhatsApp, Google Drive, uploaded files) and search across all of them from one place.

- **MVP scope:** uploaded files + Gmail. Slack, Notion, WhatsApp, and Google Drive connectors come in later phases — design connectors as a pluggable port so adding a new source doesn't require reworking the core.
- **Search:** start with keyword (full-text) search across indexed content; semantic/AI search (embeddings, vector search, LLM-powered answers) is a planned later phase. Don't let the keyword-search implementation foreclose adding semantic search later — keep indexing/retrieval behind a port.
- **Tenancy:** multi-user from the start. Every source connection, indexed document, and search result must be scoped and isolated per user (per-user OAuth tokens, no cross-user data leakage).
- **Indexing model:** content is synced from each source and indexed/stored in our own store (not queried live from source APIs at search time). Design sync/indexing as a background process per source connector, independent of the search-request path.

## Development process

- Practice spec-driven development: before writing code for a feature, write or update a spec (requirements/behavior, interfaces, edge cases) and get it agreed before implementation starts.
- Follow modern hexagonal architecture (ports & adapters): keep domain/business logic independent of frameworks and I/O; expose it through ports (interfaces); implement adapters (FastAPI routes, DB clients, external APIs, etc.) that plug into those ports. Don't let framework or infrastructure concerns leak into the domain layer.
- Always plan before implementing any feature. Lay out the approach (affected modules, ports/adapters touched, spec changes) before editing code.

## Git commits

- Do not add a `Co-Authored-By` line to commit messages.
