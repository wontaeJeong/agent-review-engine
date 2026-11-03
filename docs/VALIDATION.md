# Validation summary (2026-11-04)

## Executed in local development runtime

- `pytest -q`: **36 passed**.
- `python -m compileall -q src tests migrations`: passed.
- Initial Alembic migration applied to a fresh SQLite database: `0001_initial (head)`; created `jobs`, `deliveries`, `repository_settings`.
- App endpoints tested via FastAPI TestClient with HMAC-signed webhook fixtures and mocked GitHub API.
- Jev disabled/local-schema/low-confidence fallback, LLM structured JSON, CI gate, SHA staleness, bot approval, human escalation, auto-enqueue behavior, Merge Queue group SHA, and per-repo flags tested with fakes.

## Not executed

- Live GHES 3.19 integration, installing GitHub App / actually posting an approval or check.
- Full CI build or Kubernetes/Compose deployment (Docker and kubectl unavailable in the current runtime).
- A real local Jev endpoint or a real on-prem LLM inference call.
- Production security testing, concurrency load testing, provider token accounting, and real queue merge behavior.

Treat this package as a **validated local MVP**, not an already deployed production service.
