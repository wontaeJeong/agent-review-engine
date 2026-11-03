"""Production-hardening regression checks: profile isolation, audit and metrics."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_review_engine.app import create_app
from agent_review_engine.config import Settings
from agent_review_engine.models import JobSpec
from agent_review_engine.providers import LLMReviewer
from agent_review_engine.store import Store


@pytest.mark.asyncio
async def test_server_owned_model_profile_and_key_resolution(monkeypatch):
    monkeypatch.setenv("REVIEW_KEY_IN_VAULT_ENV", "internal-provider-token")
    settings = Settings(
        llm_base_url="http://primary.internal/v1", llm_model="primary",
        llm_profiles_json=json.dumps({"secondary": {
            "base_url": "https://model.internal/v1", "model": "reviewer-32b",
            "api_key_env": "REVIEW_KEY_IN_VAULT_ENV", "timeout_seconds": 45,
        }}),
        llm_allowed_hosts="model.internal,primary.internal",
    )
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)["model"]))
        return httpx.Response(200, json={"choices": [{"message": {"content":
            '{"verdict":"approve","summary":"passed","findings":[]}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        verdict, _ = await LLMReviewer(settings, client).review(
            title="update", description="", files=[{"filename": "a.py", "patch": "@@ -1 +1 @@\n-1\n+2"}],
            ci_summary="passed", phase="pr", profile_name="secondary")
    assert verdict.verdict == "approve"
    assert seen == [("https://model.internal/v1/chat/completions",
                     "Bearer internal-provider-token", "reviewer-32b")]


def test_untrusted_configured_url_blocked_and_profile_names_strict():
    settings = Settings(llm_base_url="http://allowed.internal/v1", llm_model="default",
                        llm_allowed_hosts="allowed.internal")
    with pytest.raises(ValueError, match="allowlisted"):
        Settings(llm_base_url="http://evil.example/v1", llm_model="m",
                 llm_allowed_hosts="allowed.internal").resolve_llm_profile()
    with pytest.raises(ValueError, match="without credentials"):
        Settings(llm_base_url="http://user:pass@allowed.internal/v1", llm_model="m").resolve_llm_profile()
    with pytest.raises(ValueError, match="Unknown"):
        settings.resolve_llm_profile("arbitrary-user-controlled-url")
    with pytest.raises(ValueError, match="Invalid LLM profile name"):
        Settings(llm_profiles_json='{"bad name":{"base_url":"http://localhost","model":"x"}}').available_llm_profiles()


def test_repo_overrides_protected_profile_selection_and_audit(tmp_path):
    config = Settings(
        database_url=f"sqlite:///{tmp_path}/reviews.sqlite", worker_enabled=False,
        repository_allowlist="org/repo", admin_metrics_token="secret-admin",
        llm_base_url="http://primary.internal/v1", llm_model="primary",
        llm_profiles_json='{"secondary":{"base_url":"http://secondary.internal/v1","model":"small"}}',
    )
    app = create_app(config, start_worker=False)
    token = {"X-Admin-Token": "secret-admin"}
    with TestClient(app) as client:
        assert client.get("/admin/audit").status_code == 403
        assert client.get("/admin/metrics").status_code == 403
        assert client.patch("/admin/repos/org/repo", headers=token,
                            json={"llm_profile": "unknown"}).status_code == 422
        assert client.patch("/admin/repos/org/repo", headers=token,
                            json={"llm_profile": "secondary", "jev_enabled": False}).status_code == 200
        repo = client.get("/admin/repos/org/repo", headers=token).json()
        assert repo["effective"]["llm_profile"] == "secondary"
        assert repo["effective"]["jev_enabled"] is False
        audit = client.get("/admin/audit?repository=org/repo", headers=token).json()
        assert audit["events"][0]["event_type"] == "policy_updated"
        assert "secret" not in json.dumps(audit)
        assert client.get("/admin/audit?limit=999", headers=token).status_code == 422
        assert client.get("/admin/metrics?days=2", headers=token).status_code == 422
        assert client.get("/admin/metrics?days=30", headers=token).json()["jobs_total"] == 0


def test_job_decision_audit_is_idempotent_and_never_contains_raw_patch(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/reviews.db")
    spec = JobSpec(kind="pr", repository="org/repo", installation_id=1,
                   sha="a" * 40, pr_number=3, trigger="pull_request.opened")
    assert store.enqueue("evt-1", [spec], 0) == 1
    state = {"verdict": {"verdict": "approve", "summary": "fine"},
             "risk": "low", "llm_profile": "secondary", "ci_summary": "light-ci passed",
             "usage": {"prompt_tokens": 30, "completion_tokens": 12},
             "raw_patch": "THIS MUST NEVER BE IN AUDIT"}
    store.complete(spec.key, 0, state)
    store.complete(spec.key, 0, state)
    events = store.audit("org/repo")
    assert len(events) == 1
    assert events[0]["details"]["llm_profile"] == "secondary"
    assert "THIS MUST NEVER BE IN AUDIT" not in json.dumps(events)
    metrics = store.metrics(7)
    assert metrics["tokens"] == {"prompt_tokens": 30, "completion_tokens": 12}
    assert metrics["verdicts"] == {"approve": 1}
    assert metrics["status"] == {"done": 1}


def test_credential_never_allowed_inline_in_profile(monkeypatch):
    monkeypatch.delenv("REVIEW_MISSING_KEY", raising=False)
    settings = Settings(llm_profiles_json='{"internal":{"base_url":"https://intranet/v1","model":"m","api_key":"oops"}}')
    with pytest.raises(ValueError, match="secret environment"):
        settings.resolve_llm_profile("internal")
    settings = Settings(llm_profiles_json='{"internal":{"base_url":"https://intranet/v1","model":"m","api_key_env":"REVIEW_MISSING_KEY"}}')
    with pytest.raises(ValueError, match="not set"):
        settings.resolve_llm_profile("internal")
