from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from agent_review_engine.app import create_app
from agent_review_engine.config import Settings


HEAD = "a" * 40
BASE = "b" * 40


def make_settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/app.db", github_webhook_secret="secret",
        github_app_id=123, github_private_key_path="/not-yet-available",
        llm_base_url="http://local-llm/v1", llm_model="review",
        admin_metrics_token="super-secret-admin-token", bridge_token="super-secret-bridge-token",
        repository_allowlist="org/repo", worker_enabled=False,
    )


def payload(action="opened", draft=False):
    return {
        "action": action, "repository": {"full_name": "org/repo"},
        "installation": {"id": 15}, "number": 5,
        "pull_request": {"number": 5, "state": "open", "draft": draft,
                         "head": {"sha": HEAD}, "title": "Change"},
    }


def webhook(client, obj, *, delivery="delivery-1", secret="secret"):
    body = json.dumps(obj).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/github", content=body, headers={
        "x-github-event": "pull_request", "x-github-delivery": delivery,
        "x-hub-signature-256": f"sha256={sig}",
    })


def test_webhook_hmac_deduplicates_and_ignores_unenrolled(tmp_path):
    app = create_app(make_settings(tmp_path), start_worker=False)
    with TestClient(app) as client:
        assert client.post("/webhooks/github", json=payload()).status_code == 401
        assert webhook(client, payload()).json()["accepted"] == 2
        assert webhook(client, payload()).json()["accepted"] == 0
        other = payload()
        other["repository"]["full_name"] = "unapproved/repo"
        assert webhook(client, other, delivery="delivery-2").json()["accepted"] == 0
        assert client.get("/healthz").json()["status"] == "alive"


def test_admin_can_toggle_jev_without_credentials(tmp_path):
    app = create_app(make_settings(tmp_path), start_worker=False)
    auth = {"x-admin-token": "super-secret-admin-token"}
    with TestClient(app) as client:
        assert client.get("/admin/repos/org/repo").status_code == 403
        initial = client.get("/admin/repos/org/repo", headers=auth).json()
        assert initial["effective"]["jev_enabled"] is False
        assert initial["effective"]["approval_mode"] == "report_only"
        result = client.patch("/admin/repos/org/repo", headers=auth,
                              json={"jev_enabled": True}).json()
        assert result["overrides"]["jev_enabled"] is True
        assert client.get("/admin/repos/org/repo", headers=auth).json()["effective"]["jev_enabled"]
        assert client.patch("/admin/repos/org/repo", headers=auth,
                            json={"approval_mode": "dangerous"}).status_code == 422
        assert client.get("/admin/summary", headers=auth).status_code == 200
        assert client.patch("/admin/repos/other/repo", headers=auth,
                            json={"jev_enabled": False}).status_code == 403


def test_bridge_uses_app_installation_and_validates_head(tmp_path):
    app = create_app(make_settings(tmp_path), start_worker=False)
    with TestClient(app) as client:
        class FakeGitHub:
            async def repository_installation(self, repo):
                return 15

            async def ref_sha(self, iid, repo, ref):
                return HEAD

        app.state.github = FakeGitHub()
        data = {"repository": "org/repo", "head_sha": HEAD, "base_sha": BASE,
                "head_ref": "refs/heads/gh-readonly-queue/main/pr-3", "base_ref": "refs/heads/main"}
        assert client.post("/bridge/merge-group", json=data).status_code == 403
        resp = client.post("/bridge/merge-group", json=data,
                           headers={"x-bridge-token": "super-secret-bridge-token"})
        assert resp.status_code == 202 and resp.json()["accepted"] == 1
        data["head_sha"] = "x" * 40
        assert client.post("/bridge/merge-group", json=data,
                           headers={"x-bridge-token": "super-secret-bridge-token"}).status_code == 422
        data["head_sha"] = "c" * 40
        assert client.post("/bridge/merge-group", json=data,
                           headers={"x-bridge-token": "super-secret-bridge-token"}).status_code == 409


def test_manual_recheck_only_ready_pr(tmp_path):
    app = create_app(make_settings(tmp_path), start_worker=False)
    with TestClient(app) as client:
        class FakeGitHub:
            draft = True

            async def repository_installation(self, repo):
                return 15

            async def pr(self, iid, repo, number):
                return {"state": "open", "draft": self.draft, "head": {"sha": HEAD}}

        fake = FakeGitHub()
        app.state.github = fake
        auth = {"x-admin-token": "super-secret-admin-token"}
        body = {"repository": "org/repo", "number": 5}
        resp = client.post("/admin/recheck", json=body, headers=auth)
        assert resp.status_code == 202 and resp.json()["accepted"] == 1
        fake.draft = False
        resp = client.post("/admin/recheck", json=body, headers=auth)
        assert resp.status_code == 202 and resp.json()["accepted"] == 2
