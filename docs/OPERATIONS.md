# Operations, diagnosis, and runbooks

## Admin endpoints (authenticated with `X-Admin-Token`)

| Operation | Endpoint |
| --- | --- |
| Liveness | `GET /healthz` (public to health probe) |
| Configuration readiness | `GET /readyz` |
| Job counts/recent errors | `GET /admin/summary` |
| Effective repo policy | `GET /admin/repos/{owner}/{repo}` |
| Per-repo feature flags | `PATCH /admin/repos/{owner}/{repo}` with `jev_enabled`, `approval_mode`, `auto_enqueue` |
| Retry exact current head | `POST /admin/recheck` with `repository`, `number` |

Admin API should be private-network only, behind internal SSO / mTLS and network policy. A token alone is not sufficient authorization for a multi-tenant organization-wide product.

## Failure guide

| Symptom | Behavior | Operator action |
| --- | --- | --- |
| Jev endpoint down/incomplete | `review:jev-degraded`, fallback labels; no impact on LLM approval | Leave Jev disabled; repair schema/auth; recheck. |
| Light CI missing | In-progress `agent-review-gate`, then blocking timeout | Check workflow job name and check source App ID. |
| Heavy CI missing on merge group | Queue's App check waits, then fails | Ensure `merge_group` Action builds the synthetic SHA. |
| LLM timeout/429/5xx | Worker retries with backoff; terminal blocking check | Validate endpoint/model/key, inspect Worker logs, manual recheck. |
| LLM invalid JSON | Blocking check, no APPROVE | Fix model JSON reliability; lower max diff or use stronger model. |
| Invalid/missing patch | Blocking `action_required` | Break up PR or use a controlled human exception outside this MVP. |
| High-risk PR | Requires authorized person on exact SHA | Human review; app re-evaluates on `pull_request_review`. |
| PR upgraded to Draft | Worker declines approval | Author marks Ready to trigger new evaluation. |
| Stale commit | No approval for old SHA | New `synchronize` event evaluates new SHA. |
| App lacks `Issues:write` | Label job fails | Correct App permissions; re-deliver webhook / manual recheck. |
| App bot APPROVE not counted | GitHub still blocks merge | Validate protected branch required reviewer policy; keep App Check required, do not bypass. |
| Queue GraphQL rejects | Gate may pass, `enqueue_status=pending` | Other required reviews/checks may be pending; recheck after satisfying rules. |
| Queue SHA changes/deleted | Old job becomes stale | Wait for new group and its own heavy CI. |

## Persistent queue notes

- Job key: `<repository>:<pr|group>:<phase>:<head_sha>`. Webhook `X-GitHub-Delivery` deduplicated.
- PostgreSQL jobs claimed with row locks, lease expiry 10m, generation checks prevent old workers overwriting a newer recheck.
- Fail closed after `MAX_ATTEMPTS`. On terminal failure, attempt to publish a blocking GitHub Check and keep the job status `blocked`.
- If GHES is unavailable, the terminal Check might not be published. A missing required check still blocks merging; alert on prolonged pending Checks.
- Do not persist API keys, raw diffs, or full LLM prompts in the job database. Job records contain parsed verdict, usage and operational metadata.
- Audit events, retention/deletion, Prometheus metrics, alerting, cost reports, a full access-controlled dashboard, and hardened lease renewal are follow-up production requirements.

## Monitoring priorities

- Event-to-review-complete latency (p50/p95/p99), review timeout/failure and pending jobs, queue wait, webhook error rate.
- PRs per risk/area, human-escalated share, invalid JSON share, false positive/false negative discovered in retrospective tests.
- Token counts by repo/model/phase, total API requests, on-prem model latency and throughput; no on-prem unit price inferred unless configured.
- Merge Queue rebuild/cancellation rate, CI retry count, stale verdict attempts, App token/permission error rate.
