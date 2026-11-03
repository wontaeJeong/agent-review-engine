# agent-review-engine

**Central GitHub App review service** for agent-first teams using GitHub Enterprise Server 3.19, with optional Jev labels, a locally hosted OpenAI-compatible LLM, required Checks, and Merge Queue validation.

> **MVP / pilot-ready code, not a production security certification.** Run in `report_only` mode first. Do not change protected-branch requirements until the behavior is verified on your GHES instance. The LLM's `APPROVE` is distinct from GitHub's merge permission.

## Decisions

| Question | Default / answer |
| --- | --- |
| When to review? | Non-draft `opened`, `ready_for_review`, new `synchronize`, `reopened`, relevant `edited`; light CI must pass **before** the LLM runs. |
| What does Jev do? | **Advisory labels only**. Global + per-repo `jev_enabled` (default **false**); failure falls back to path classification and never blocks the review. |
| Who approves? | On-prem LLM returns structured verdict; **GitHub App** posts an actual `APPROVE` review only if central policy passes. |
| What's required? | App-owned `agent-review-gate` check on **both PR HEAD SHA and Merge Queue HEAD SHA**. |
| Heavy CI? | On Merge Queue synthetic commit. Fail-closed, LLM reviews the combined diff, **no second PR approval**. |
| Auto-queue? | Optional `auto_enqueue` (default **false**), via GitHub GraphQL `enqueuePullRequest` with `expectedHeadOid`. Never bypasses branch rules. |
| Local LLM? | `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` configured separately. No provider-specific SDK required. |

## Processing paths

```text
PR created (Draft) ──> deterministic / optional Jev labels
        │
        ├─ ready_for_review ─────┐
        └─ PR created as Ready ──┤
                                 v
                          Wait for Light CI
                                 │
                                 v
                        Central risk policy
                                 │
                                 v
                        On-prem LLM review
                          │           │
                     eligible       failed/uncertain
                          │           └─> block, comment, recheck
                          v
                 GitHub App APPROVE (unless human required)
                          │
                          v
                 agent-review-gate = success
                          │
                          v
              optional enqueuePullRequest
                          │
                          v
          GitHub Merge Queue synthetic SHA
                     │             │
                   Heavy CI     LLM review
                     └──────┬──────┘
                            v
                   required check = success
                            v
                     GitHub merges
```

### Trigger matrix

| Event | Labels | LLM/Check | Notes |
| --- | --- | --- | --- |
| `pull_request.opened`, Draft | Yes | No | No wasteful review while coding. |
| `pull_request.opened`, Ready | Yes | Yes, after Light CI | Handles PRs created ready immediately. |
| `ready_for_review` | Existing metadata | Yes | Main approval trigger. |
| `synchronize` | Refresh | Re-review new SHA | Old approval/check do not authorize new SHA. |
| `reopened` | Refresh | Yes if Ready | |
| `edited` (title/body/base) | Refresh | Re-evaluate | Existing metadata-sensitive verdict invalidated. |
| `converted_to_draft` | Keep | Do not approve | Worker re-reads draft flag before posting. |
| `check_run.completed` for named CI | No | Recheck | Handles rerun after failing check. |
| Human `pull_request_review` | No | Recheck | Only for a human; avoid bot feedback loops. |
| Queue temporary branch `push` | No | Heavy CI + combined LLM | GHES 3.19 third-party CI fallback. |
| `merge_group` via Action bridge | No | Heavy CI + combined LLM | Exact `base_sha` and `head_sha` preferred. |

## Quick start

Requires Python >=3.12 and a reachable GHES instance. For local development:

```bash
uv sync --extra dev
cp .env.example .env
# set GITHUB_*, LLM_*; leave JEV_ENABLED=false
uv run uvicorn agent_review_engine.app:app --host 0.0.0.0 --port 8080
# another terminal
uv run python -m agent_review_engine.worker
uv run pytest -q
```

Running the server alone does **not** start the worker unless `WORKER_ENABLED=true`. For a quick single-process demo, set `WORKER_ENABLED=true` with one Uvicorn process and SQLite. For real deployments use PostgreSQL, one or more independent worker instances, and database migrations.

The service does **not** run PR code or clone untrusted repositories. Tests and builds remain in isolated GitHub Actions runners.

### Example deployment

```bash
cp .env.example .env
# configure GHES URL, App ID/private key, webhook secret, LLM credentials, DB URL
# put App PEM under ./secrets/github-app-private-key.pem
# set POSTGRES_PASSWORD in the environment; DATABASE_URL to postgresql+psycopg://...
docker compose up --build -d
```

See [DEPLOYMENT.md](docs/DEPLOYMENT.md) for GitHub App permissions, webhook subscription, branch protection configuration and Kubernetes notes.

### Repository-specific Jev toggle

The admin API requires `ADMIN_METRICS_TOKEN`. No API key is stored in Repository settings.

```bash
# global JEV_ENABLED=false remains the default
curl -H "X-Admin-Token: $ADMIN_METRICS_TOKEN" \
  https://review.internal/admin/repos/org/repo

# Enable Jev on one repository when local endpoint becomes ready
curl -X PATCH https://review.internal/admin/repos/org/repo \
  -H "X-Admin-Token: $ADMIN_METRICS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"jev_enabled":true}'

# Disable again without stopping review or changing the LLM endpoint
curl -X PATCH https://review.internal/admin/repos/org/repo \
  -H "X-Admin-Token: $ADMIN_METRICS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"jev_enabled":false}'

# Re-evaluate an already open PR (also refreshes labels)
curl -X POST https://review.internal/admin/recheck \
  -H "X-Admin-Token: $ADMIN_METRICS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"repository":"org/repo","number":123}'
```

`jev_enabled` is nullable in the per-repo overrides: `null` means inherit the global default. In the disabled/degraded state, path-based labels remain; the LLM review **does not wait for Jev**.

Jev's risk label is `jev:risk-*`, deliberately separate from the centrally enforced `risk:*` label. The Jev advisory cannot override hard security policy. If the optional classifier is unavailable, `review:jev-degraded` is added for visibility.

### Production hardening branch (`feat/production-hardening`)

This branch adds capabilities **beyond the tagged MVP**:

- Per-repository LLM model selection through **server-provisioned profiles**. `LLM_PROFILES_JSON` stores endpoint/model and a secret **environment variable name**; it never accepts inline API keys or arbitrary URLs from PR text. Optionally restrict model destinations with `LLM_ALLOWED_HOSTS`.
- Durable, append-only audit records for final review decisions and repository policy edits (`audit_events`), without storing diffs, model prompts or raw CI text. Decision evidence is tied to repository and SHA.
- Authenticated `GET /admin/audit` and `GET /admin/metrics?days=7|30|90` APIs, including verdict distribution and input/output token usage.
- Database migration `0002_audit_profiles`. Apply `alembic upgrade head` **before** deploying the advanced branch to an existing MVP database.
- New profile, secret-isolation and audit tests, plus an explicit CI lint rule set.

Example server environment and per-repo selection:

```bash
export FAST_REVIEW_KEY="$(cat /run/secrets/fast-llm-key)"
export LLM_PROFILES_JSON='{"fast":{"base_url":"https://vllm.internal/v1","model":"review-small","api_key_env":"FAST_REVIEW_KEY"}}'
export LLM_ALLOWED_HOSTS='litellm.internal,vllm.internal'

curl -X PATCH https://review.internal/admin/repos/org/repo \
  -H "X-Admin-Token: $ADMIN_METRICS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"llm_profile":"fast","jev_enabled":false}'
```

Settings are server-owned, not controlled by PR authors. The `default` profile continues to use existing `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` for backward compatibility. Admin token authentication is still an MVP limitation; deploy behind SSO-aware ingress and plan per-role auth before team-wide production.

### Turn on actual approval / optional auto-queue

`APPROVAL_MODE=report_only` initially produces blocking `action_required` checks and **no GitHub APPROVE**. Do not make `agent-review-gate` required during a shadow pilot, or the pilot will block merges. To activate after a GHES pilot:

```bash
curl -X PATCH https://review.internal/admin/repos/org/repo \
  -H "X-Admin-Token: $ADMIN_METRICS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"approval_mode":"approve","auto_enqueue":true}'
```

`auto_enqueue` uses the GitHub GraphQL `enqueuePullRequest` mutation, not a direct merge. If additional branch requirements are unmet, the check can still pass while queue admission stays pending; operators can inspect `/admin/summary` or recheck after other approvals.

## Security invariants

- GitHub App webhook requests require verified HMAC-SHA256; duplicate deliveries are stored idempotently.
- Only repositories on `REPOSITORY_ALLOWLIST` are processed (recommended in production).
- GitHub App and LLM keys remain server-side, never in PR workflow inputs or PR content.
- GitHub PR text and diffs are explicitly treated as untrusted, not instructions to the reviewer.
- Diff truncation, missing patches, unexpected model JSON, and CI failure **cannot** approve a PR.
- Authorization is decided by immutable service policy and trusted CI, **not by labels** or LLM text alone.
- Security/CI/infrastructure/DB migrations and forks require a human; CODEOWNERS and branch-protection reviews remain authoritative.
- On failure, check conclusion is `failure` or `action_required`, not `skipped`/`neutral`.
- Merge Queue validates the **group SHA**, not the PR HEAD SHA, before GitHub merges.

## Layout

```text
src/agent_review_engine/
  app.py          HTTP webhook / admin endpoints / bridge
  worker.py       Dedicated worker entrypoint
  events.py       Event router
  store.py        Persistent jobs, deliveries, repo overrides
  engine.py       Review state machine + GitHub side effects
  providers.py    Jev adapter + OpenAI-compatible LLM adapter
  policy.py       Deterministic policy + fallback labeling
  github.py       GitHub App REST/GraphQL API
  security.py     HMAC signature validation
examples/          GHES merge-group bridge workflow
   .
docs/             Architecture, deployment, flows, limitations
```

### Read before production

- [FLOW_AND_REQUIREMENTS.md](docs/FLOW_AND_REQUIREMENTS.md) — workflows, edge cases and acceptance criteria
- [DEPLOYMENT.md](docs/DEPLOYMENT.md) — GitHub App setup, security, CI, deployment
- [OPERATIONS.md](docs/OPERATIONS.md) — failures, retries, monitoring and rollout
- [KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) — remaining work before organization-wide rollout

