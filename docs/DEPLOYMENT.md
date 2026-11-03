# Deployment / GitHub Enterprise Server 3.19 integration

## 1. Create a GitHub App

Install on the explicitly selected repositories and use a generated RSA private key. Register the service's public, TLS-protected `POST /webhooks/github` URL and a random webhook secret.

Minimum repository permissions:

- **Metadata:** Read (implicit)
- **Contents:** Read (commit/compare data)
- **Pull requests:** **Read and write** (review `APPROVE`, list PRs/changed files)
- **Checks:** **Read and write** (publish authoritative `agent-review-gate`)
- **Issues:** **Read and write** (create/apply PR labels)

Subscribe to **pull_request**, **pull_request_review**, **check_run**, and **push** events. Restrict installation and server `REPOSITORY_ALLOWLIST`. Configure `GITHUB_BOT_LOGIN` to the login shown for your installation (used for duplicate approval detection).

**Important:** GHES 3.19 documents the `merge_group` **Actions trigger**, but does not list it as a general GitHub App webhook event. Prefer the GHES-specific `push` to `gh-readonly-queue/...` branch as an external CI trigger, or add the [Action bridge](../examples/merge-group-bridge.yml) to provide the exact queue base/head SHA. Do not assume the current GitHub Cloud App webhook behavior exists on GHES 3.19.

## 2. Configure protected branches

1. Protect `main` or target branches and **Require merge queue**.
2. Set required status check **`agent-review-gate`**, source **the review-engine GitHub App**, after active mode has been verified. This single check reports on PR SHA and queue SHA.
3. Define Light CI job names for PR and Heavy CI job names for `merge_group` as the engine's named prerequisites. See [CI example](../examples/ci-light-heavy.yml).
4. **Do not require a PR-only check directly on merge_group SHA** (or queue-only check on PR SHA); both phases use the same branch-protection requirements. Either publish those jobs on both SHAs or make only the combined App gate required, with the engine enforcing both phase-specific checks internally.
5. Consider 'Dismiss stale pull request approvals', CODEOWNERS, latest-push approval and conversation resolution rules. These remain independent of the engine's output.
6. Restrict admin bypass and use protected immutable branches and workflow files. A PR author must not be allowed to edit the service's server-side policies.

## 3. Configure settings

Populate a `.env` based on [the example](../.env.example):

```ini
GITHUB_API_URL=https://github.company/api/v3
GITHUB_APP_ID=12345
GITHUB_PRIVATE_KEY_PATH=/run/secrets/github-app-private-key.pem
GITHUB_WEBHOOK_SECRET=...long-random-secret...
GITHUB_BOT_LOGIN=agent-review-engine[bot]
REPOSITORY_ALLOWLIST=platform/repo-a,platform/repo-b

# Only these are sent to the local LLM; no keys in individual PR workflows.
LLM_BASE_URL=https://llm-gateway.internal/v1
LLM_MODEL=my-reviewed-local-model
LLM_API_KEY=...local-token...

JEV_ENABLED=false
JEV_API_URL=http://jev.internal/v1/systemone
JEV_API_KEY=

LIGHT_CI_CHECKS=light-ci
HEAVY_CI_CHECKS=heavy-ci
CHECK_NAME=agent-review-gate
APPROVAL_MODE=report_only
AUTO_ENQUEUE=false
ADMIN_METRICS_TOKEN=...long-random-admin-token...
```

The Jev adapter uses a **TypeSafe-compatible `POST /v1/systemone` request/response schema**. A custom local endpoint must implement that schema to be enabled successfully. Endpoint incompatibility is handled by deterministic fallback, not by approval bypass. Jev receives PR metadata/path names (and optionally description), **not full diffs**.

If GHES, LiteLLM or vLLM endpoints use a private corporate CA, provide a trusted CA bundle (e.g. `SSL_CERT_FILE=/etc/ssl/certs/internal-ca-bundle.pem`) in API and Worker containers. Do not bypass TLS certificate validation; mount CA files as ConfigMap/Secret and restart pods.

In active approval mode set **`CI_EXPECTED_APP_ID`** to the trusted producer of Light/Heavy CI checks. The service refuses to become ready in approval mode without it. The current MVP supports one expected App ID for both phases; split provider identities require an enhancement before deployment.

`LLM_BASE_URL` is the OpenAI-compatible base URL (for example `/v1`); the implementation calls `/chat/completions`. `LLM_API_KEY` can be empty for a trusted internal no-auth proxy, but authenticating and TLS/mTLS are strongly recommended.

## 4. Database & runtime

**Development:** SQLite and `SCHEMA_AUTO_CREATE=true` with a single Uvicorn instance. Set `WORKER_ENABLED=true` for all-in-one smoke testing, or run `python -m agent_review_engine.worker` separately.

**Deployment:** PostgreSQL with `SCHEMA_AUTO_CREATE=false` and Alembic migration executed before API/workers. An example Compose stack is provided at `docker-compose.yml`. With the application installed, run:

```bash
alembic upgrade head
uvicorn agent_review_engine.app:app --host 0.0.0.0 --port 8080
python -m agent_review_engine.worker
```

Kubernetes: deploy PostgreSQL separately (e.g. CNPG), API Deployment, Worker Deployment, Secret volume for private PEM, Secret environment for credentials, internal Service and a restricted HTTPS Ingress. Run `alembic upgrade head` as a **Job before rollout**. One or more Workers can process PostgreSQL queue leases using `FOR UPDATE SKIP LOCKED`; SQLite multi-worker is not supported.

**Readiness:** `/readyz` checks whether essential configuration is present, not every dependency's liveness; enable database/LLM synthetic checks and alerts before a high-availability rollout.

## 5. Merge Queue Action bridge (optional)

Add `examples/merge-group-bridge.yml` to each target repo only if your GitHub App does not consistently see `gh-readonly-queue/...` push events. Configure repository variable `REVIEW_ENGINE_URL` and secret `REVIEW_ENGINE_BRIDGE_TOKEN` matching central `BRIDGE_TOKEN`.

The Action sends only repository name, group SHA, base SHA and refs to `/bridge/merge-group`. Central service uses **App JWT to fetch the installation ID** and verifies the queue ref resolves to the submitted SHA. The Action does **not** receive LLM credentials and does **not** checkout PR code. Use pinned Actions and a trusted self-hosted runner image, and restrict outbound network paths.

## 6. Rollout

1. Start in `report_only` without adding the gate to branch protection. Capture review quality without modifying approvals.
2. Verify 20–50 real PRs including modified commits, infrastructure changes, forks, bot PRs, CI reruns and merge groups.
3. Validate whether this GHES installation counts GitHub App `APPROVE` for the repository's actual Required Reviews policy. This is **not assumed** by the service.
4. Activate `approval_mode=approve` on a test repository. Then require the check from the correct GitHub App.
5. Turn on `auto_enqueue` only after checking branch protection enforces a Merge Queue. Do not use this feature on an unprotected branch.
6. Expand rollout by repository, measure false approvals and manual intervention, and keep an operator rollback switch (`approval_mode=report_only`, or remove gate protection under a documented change window).
