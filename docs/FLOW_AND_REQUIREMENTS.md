# User workflows, acceptance criteria, and requirement review

Scope: GitHub Enterprise Server 3.19; team members and coding agents work in shared repositories. Human review focuses on evidence summaries and exceptions, not routine line-by-line inspection.

## User roles

| Role | Entry point | Sees / does | Exit condition |
| --- | --- | --- | --- |
| Agent/developer | PR `Draft` | Keep pushing code; sees `area:*`, `risk:*`, optional `jev:risk-*`. | Marks Ready; no LLM charges yet. |
| PR author | Ready PR | Checks, review verdict, blocking findings. | Fixes code/pushes new SHA or requests human exception. |
| Automated reviewer | Central Worker | Diff, trusted Light CI state, hard policy; posts model review. | Successful GitHub App Check + optional `APPROVE`. |
| Human reviewer | High-risk PR | Auth/permissions, infra, migrations, forks, CODEOWNERS. | Provides authorized human review on exact SHA; central job reevaluates. |
| Merge Queue | Group HEAD SHA | Heavy CI + integration LLM verdict. | GitHub merges only when all required checks succeed. |
| Repo maintainer | Admin API / GHES settings | Chooses Jev on/off, shadow/approve, auto-enqueue; branch protection. | Desired policy takes effect; recheck existing PRs. |
| Platform operator | `/admin/summary` + logs | 429/5xx, CI timeouts, failed jobs, LLM token usage. | Reruns jobs or fixes provider; no backdoor success check. |

## State machine

```text
DRAFT ──ready_for_review──> WAIT_LIGHT_CI
  ^                               │
  └─converted_to_draft─────────────┤
                                  v
                                REVIEW
                      ┌───────────┴─────────┐
                      │                     │
                ELIGIBLE               BLOCKED
                      │                     │
                  APPROVED                FIX/PUSH
                      │                     │
                   CHECK_OK <────── new SHA ┘
                      │
                 [AUTO_ENQUEUE]
                      │
                   QUEUED
                      │
               WAIT_HEAVY_CI
                      │
                GROUP_REVIEW
                  │        │
                PASS      FAIL
                  │        └─> GitHub removes group; author fixes PR
                  v
             GitHub MERGES
```

Internally, Jobs are SHA-scoped and phases (`label`, `pr`, `queue`) have separate keys. GitHub App checks are separate from PR `APPROVE` reviews. Queue-side LLM **does not** submit a PR approval review for the synthetic queue commit.

## Entry points / expected behavior

1. **PR created in Draft:** label job only, even if Light CI is already running. `ready_for_review` starts the LLM job.
2. **PR created Ready:** `opened` schedules label and review; debounce reduces duplicate effort; review waits for Light CI.
3. **More commits:** `synchronize` schedules new SHA-scoped jobs. Previous success for an older SHA is never accepted as success for the new SHA.
4. **PR moved back to Draft:** pending job checks live PR status before review or approval. On becoming Ready it can be rechecked.
5. **PR edited, reopened, or CI rerun:** targeted recheck; metadata edit discards prior cached verdict. Exact CI-name check_run events requeue failed/pending work.
6. **Jev disabled:** no external Jev call; deterministic area/risk labels; reviewer proceeds independently.
7. **Jev enabled but incompatible/down:** `review:jev-degraded`; deterministic labels; reviewer still proceeds. Incomplete JSON and low confidence are degraded, not approvals.
8. **LLM endpoint down/unreliable:** retry with backoff, then fail the app-owned Check. No automatic approval; preserve stale SHA protection.
9. **Large/binary/undecodable diff:** cannot be safely auto-reviewed; blocks with reason rather than inventing a verdict.
10. **High-risk/fork PR:** deterministic policy requires authorized human review of the exact SHA; model alone cannot unlock the required Check.
11. **Existing CODEOWNERS/branch rules:** GitHub remains authoritative. No review bot can bypass those rules.
12. **Post-review auto-enqueue:** optional per-repo. Use GraphQL `enqueuePullRequest(expectedHeadOid)`, never direct merge or administrator bypass.
13. **Merge Queue:** run Heavy CI on synthetic SHA and review combined diff. Queue branch recreation generates a different SHA, which is independently validated.
14. **Queue branch deleted/stale:** job becomes stale, not successfully approved. A missing base/oversized group diff is blocked.
15. **Operator recheck:** `POST /admin/recheck` is authenticated and uses actual GHES HEAD SHA, never a client-supplied SHA.

## CI / risk policy

- `LIGHT_CI_CHECKS`: exact successful check-run names on PR HEAD.
- `HEAVY_CI_CHECKS`: exact successful check-run names on Queue HEAD.
- `CHECK_NAME`: central `agent-review-gate`, must differ from all CI checks.
- If `CI_EXPECTED_APP_ID` is set, status from another App or arbitrary commit status cannot satisfy the configured CI checks.
- Security-relevant paths trigger high risk regardless of Jev/LLM output. Medium-risk autoapproval can be disabled globally.
- PR labels are untrusted metadata. A repo collaborator adding `risk:low` cannot change central Policy Engine decisions.
- Model verdict `approve` + any finding >=medium is invalid/blocked; JSON schema and finding paths are checked.
- GitHub App installation tokens have short TTL; bot review idempotency uses HEAD SHA and rechecks live PR state.

## Acceptance criteria

| ID | Requirement | MVP validation |
| --- | --- | --- |
| R01 | Ready first or draft->ready runs once per SHA | Tests + event routing |
| R02 | Jev globally disabled by default | Settings test |
| R03 | Jev per-repo toggle independently of LLM | Admin API tests |
| R04 | Jev outage does not block LLM | Fallback policy & regression tests |
| R05 | CI failure never approves | CI-gating test |
| R06 | AI review cannot approve stale SHA | SHA mutation test |
| R07 | High risk requires human | Policy test |
| R08 | No wrong Check success on group SHA | Queue test |
| R09 | Webhook signature and replay resistant | HTTP API tests |
| R10 | Optional queue admission respects expected SHA | GraphQL client and policy |
| R11 | Same Check appears on PR and Merge Queue SHAs | Workflow contract; live GHES verification still required |
| R12 | GitHub App `APPROVE` meets required reviews (if enabled) | **Must confirm on target GHES** |
| R13 | LLM failure recovers without duplicate bot spam | Idempotency + worker tests; chaos test pending |
| R14 | Signed, immutable engine deployment and protected App secrets | Production hardening pending |

## UX suggestions (not included in MVP web UI)

On PR, use the native GitHub Checks surface as the primary UI: **Risk & area**, **CI proof**, **LLM verdict**, **blocking findings**, **What to do next**, and **Queue status**. Keep summaries short enough to be read without a source-code diff. Provide links to logs and full evidence outside Checks when the service has an authenticated dashboard.

A future dashboard should include repo-level policies, rate/latency/cost trends, failed approval cases, override requests, and model drift monitoring. This MVP supplies the foundation (job metadata and admin API) but does not claim a complete operator dashboard.
