# Production readiness and rollout plan

> Feature branch scope: auditable policy decisions, model profile selection, usage metrics and stronger configuration controls. **Not a claim of GHES-in-the-loop validation or production certification.**

## End-user workflows

| Journey | Expected behavior | Required evidence |
| --- | --- | --- |
| Agent opens Draft PR | Path-based labels are available; LLM review is not scheduled | Label job, webhook receipt |
| Developer marks Ready | Light CI is required before LLM call | CI check App ID, head SHA, review verdict |
| PR opens directly Ready | Same as `ready_for_review` | Initial opened event handled |
| PR gets new commit | Old SHA is never accepted as latest | New review decision and fresh check |
| PR returns to Draft | Automatic approval stops, even if existing job is in flight | Fresh GitHub PR state fetched before review action |
| Jev disabled or times out | Deterministic fallback labels; LLM still runs | `jev_status` metadata |
| LLM rejects or times out | Fail closed; no automatic approval | Failed/action_required check, retry record |
| High-risk path, fork or special ownership | Human review is required | Human approval on exact SHA; CODEOWNERS remain enforced by GitHub |
| Reviewer requests manual retry | Safe recheck of current head | Admin audit record and fresh job |
| Merge Queue builds multiple PRs | Heavy CI plus LLM on **synthetic group SHA** | Exact base/head refs, source-verified CI |
| Merge Queue rebuilt or canceled | Old queued work becomes stale | Group-ref freshness check |
| Internal LLM gateway unavailable | No unverified automatic success | Error and eventual terminal check |
| Profile changed while job in flight | New jobs use new model; already-computed verdict not silently rewritten | Audit with profile name, explicit recheck required |

## API and permissions

- GitHub App should have minimal permissions: repository metadata read, pull requests read/write (review and labels), checks read/write, and actions/commit statuses read as required for your GHES configuration.
- Create a **separate GitHub App** for check results, not a shared PAT. The service must never execute PR-supplied shell commands.
- Restrict service admin endpoints behind trusted network/SSO ingress; the included static admin token is a pilot mechanism. Restrict `/bridge/merge-group` to trusted Actions Runner networking and scoped token.
- Refuse invalid model URL configurations. `llm_profile` supplied by an admin chooses only from `LLM_PROFILES_JSON`; the PR cannot select a remote endpoint.
- Do not store API keys in repository settings, workflow inputs, audit events, PR labels, or model transcripts.

## Release gates before `approval_mode=approve`

1. Run GitHub App on a staging GHES installation and validate `APPROVE` review behavior, Check Run origin and protected branch restrictions.
2. Pin trusted check producer identity, and verify duplicate status contexts do not bypass policy.
3. Keep approval in `report_only` initially, compare with human code reviews, and measure false-approve rate on known defect PRs.
4. Validate no auto approval on updated SHAs, Draft regression, missing patches, workflow changes and large diffs.
5. Test server restarts, PostgreSQL failover, lease expiration and retry idempotence with multiple Workers.
6. Observe LLM input/output token rates and service cost; set rate limits in the gateway and alert on budget changes.
7. Provide a documented manual exception flow with reviewer identity, rationale, expiry and audit before permitting exceptions.
8. Use TLS to GitHub/LLM, non-root containers, network egress allowlists, secret mounts and a verified image provenance chain.
9. Disable preview/draft branch protection until required Check Run creation is confirmed at GHES `merge_group` synthetic SHAs.
10. Verify rollback: set `APPROVAL_MODE=report_only` or remove required policy, then recover stuck queues safely.

## Observability

- `GET /admin/metrics?days=1|7|30|90`: jobs by type/state, review verdict counts, token usage totals for completed/active jobs in the window.
- `GET /admin/audit?repository=org/repo&limit=100`: recent policy changes and per-SHA decisions, without raw diff content.
- Include Job keys, SHA and GHES event identifiers in request logs. Do not log key material or submitted patch text.
- Metrics here are durable operational counters, **not Prometheus/SLO exporters**. Add scrape endpoints, rate-limit metrics, queue age, latency histograms and alerting before scale out.

## Database upgrade / rollback

1. Back up PostgreSQL, then run `python -m alembic upgrade head` as a migration Job.
2. Roll out API servers first and Workers second, pinned to the same application version.
3. Confirm `audit_events` and nullable `repository_settings.llm_profile` exist; test `/admin/metrics` and `/admin/audit` with token.
4. Roll back application code with the schema left in place when possible; downgrade only with a validated DB backup. `0002` downgrade removes audit history — do not use casually.
5. On bad model profile or suspect results, set repository `approval_mode=report_only`, recheck targeted PRs, and disable auto enqueue.
