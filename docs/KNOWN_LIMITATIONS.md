# MVP limitations and release gates

The source package is an executable pilot scaffold. It does **not** claim production deployment, an installed GitHub App, or verified acceptance in a specific GHES instance.

## Must resolve / validate before organization-wide required-check rollout

1. **Real GHES integration:** create/install a GitHub App; confirm API permission scopes, actual bot login, webhook payload shapes, Check runs and `APPROVE` behavior under your precise branch rules. Source tests are mocked; there is no live GHES e2e proof.
2. **Jev local server schema:** the adapter expects a TypeSafe-compatible `systemone` contract. If your local server is incomplete, leave `jev_enabled=false` (default). A future custom adapter may be needed.
3. **Over-limit and binary diffs:** deliberately fail closed. Need a governed human-override process with distinct approvers, expiry, immutable SHA and audit for unusual changes; not present in this MVP.
4. **CI identity trust:** `CI_EXPECTED_APP_ID` is global, and not all organizations' checks are produced by one App. Multi-app allowlists and per-repo check policies are follow-up work. The service now requires this trusted producer ID before active approval is considered ready; a status context with an identical name cannot count while it is configured.
5. **Denylist is not complete:** deterministic high-risk path matching is conservative but cannot prove an arbitrary code change is safe. Add test-backed repository-specific rules and high-risk component ownership.
6. **Merge Queue push inference:** queue branch ref to base branch parsing is a fallback; the Action bridge with supplied exact `base_sha` is preferred. Test branch naming and multiple-PR groups on your GHES version.
7. **High-risk human approval:** current check looks for an authorized non-bot reviewer of the exact SHA. It does not substitute for CODEOWNERS and GHES Required Reviews policy and is not a four-eyes exception service.
8. **Model verification:** a single LLM JSON verdict is not proof of correctness. Add multiple independent reviewers, adversarial evaluation, calibrated thresholds, flaky tests, and incident retrospectives before granting broad autonomy.
9. **Operations:** no frontend dashboard, SSO RBAC, per-repo LLM profile secret management, Prometheus metrics, job retention, structured audit events, configurable retry backoff by provider, or Webhook rate limiting yet.
10. **Security:** isolated environment/egress allowlist, SSRF restrictions for admin-configurable model endpoints, Secret Manager integration, supply-chain provenance and internal penetration tests remain to be completed.
11. **Queue controls:** `auto_enqueue` only tries `enqueuePullRequest` once per successful job evaluation. If other required rules aren't satisfied it records a pending status; a new human review/check_run or admin recheck can retry. Durable event-driven queue retry is a follow-up.
12. **GitHub check and review atomicity:** API writes are not atomic. The service binds decisions to SHA, rechecks PR live status, and avoids duplicate approvals; check and approval posting may still race a new push, which is why GitHub protected-branch SHA-specific checks must remain enabled.

## Recommended next milestone

Build an operator UI with persisted immutable review evidence, safe provider profiles (`base_url`, `model`, secret reference separated), per-repo guardrail configuration, real-time health alerts, authorized human exception workflow, and a GHES-in-the-loop test harness. Expand beyond pilot only after measured false-approval rates satisfy your service-level policy.

## Progress in feature/production-hardening

The feature branch addresses part of MVP items 9/10: a DB-backed audit trail, token metrics, and server-defined per-repository model profiles with allowlisted hosts. These are incremental safety features, **not** an authorization boundary equivalent to enterprise SSO. Remaining high-priority work: SSO/RBAC, comprehensive metrics exporters/alerts, reconciliation after missed events, load testing, independent model-review evaluations, and live GHES 3.19 integration.
