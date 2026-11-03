from __future__ import annotations

import httpx
import pytest

from agent_review_engine.config import Settings
from agent_review_engine.github import GitHubAPI


@pytest.mark.asyncio
async def test_ci_requires_exact_success_not_neutral_skipped_or_name_collision():
    settings = Settings(ci_expected_app_id=42)
    api = GitHubAPI(settings)

    async def fake_checks(iid, repo, sha):
        return [
            {"name": "light-ci", "status": "completed", "conclusion": "skipped", "app": {"id": 42}, "id": 1},
            {"name": "light-ci", "status": "completed", "conclusion": "success", "app": {"id": 99}, "id": 2},
        ]

    async def fake_request(iid, method, url, **kwargs):
        return {"statuses": [{"context": "light-ci", "state": "success"}]}

    api.checks = fake_checks
    api.request = fake_request
    state, detail = await api.ci_state(7, "org/repo", "a" * 40, ["light-ci"])
    assert state == "failure" and "skipped" in detail
    await api.close()


@pytest.mark.asyncio
async def test_graphql_expected_head_oid_and_no_direct_merge():
    requests = []

    def transport(request):
        requests.append(request)
        assert str(request.url) == "https://github.internal/api/graphql"
        payload = __import__("json").loads(request.content)
        assert "enqueuePullRequest" in payload["query"]
        assert "mergePullRequest" not in payload["query"]
        assert payload["variables"]["input"]["expectedHeadOid"] == "a" * 40
        return httpx.Response(200, json={"data": {"enqueuePullRequest": {"mergeQueueEntry": {"id": "Q1"}}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        api = GitHubAPI(Settings(github_api_url="https://github.internal/api/v3", github_graphql_url="https://github.internal/api/graphql"), client)

        async def fake_token(iid):
            return "installation-access-token"

        api.token = fake_token
        await api.enqueue_pull_request(7, "PR_123", "a" * 40)
    assert len(requests) == 1


def test_ci_name_configuration_must_not_include_the_gate():
    settings = Settings(light_ci_checks="light-ci,agent-review-gate", heavy_ci_checks="heavy-ci",
                        llm_model="x", llm_base_url="http://local/v1", github_app_id=1,
                        github_private_key_path="/keys/app.pem", github_webhook_secret="secret")
    assert any("CHECK_NAME" in item for item in settings.missing())


def test_active_approve_requires_trusted_ci_app():
    settings = Settings(approval_mode="approve", llm_model="x", llm_base_url="http://local/v1",
                        github_app_id=1, github_private_key_path="/keys/app.pem",
                        github_webhook_secret="secret", light_ci_checks="light-ci",
                        heavy_ci_checks="heavy-ci")
    assert "CI_EXPECTED_APP_ID (required for active approval)" in settings.missing()
    settings.ci_expected_app_id = 123
    assert "CI_EXPECTED_APP_ID (required for active approval)" not in settings.missing()
