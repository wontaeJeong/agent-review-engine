from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .engine import ReviewEngine
from .events import route_event
from .github import GitHubAPI
from .models import JobSpec
from .providers import JevClassifier, LLMReviewer
from .security import validate_signature
from .store import Store

log = logging.getLogger(__name__)
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_PATTERN = re.compile(r"^[a-fA-F0-9]{40}$")


class RepoSettingsPatch(BaseModel):
    jev_enabled: bool | None = None
    auto_enqueue: bool | None = None
    approval_mode: str | None = Field(default=None, pattern="^(report_only|approve)$")
    llm_profile: str | None = Field(default=None, max_length=48)


class BridgeGroup(BaseModel):
    repository: str
    head_sha: str
    base_sha: str
    head_ref: str = ""
    base_ref: str = ""


class RecheckRequest(BaseModel):
    repository: str
    number: int = Field(ge=1)


def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> FastAPI:
    settings = settings or get_settings()
    store = Store(settings.database_url, auto_create=settings.schema_auto_create)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = httpx.AsyncClient(timeout=35, follow_redirects=False)
        github = GitHubAPI(settings, client)
        engine = ReviewEngine(settings, store, github, JevClassifier(settings, client),
                              LLMReviewer(settings, client))
        stop = asyncio.Event()
        task = asyncio.create_task(engine.worker(stop)) if (start_worker and settings.worker_enabled) else None
        app.state.engine = engine
        app.state.github = github
        try:
            yield
        finally:
            stop.set()
            if task:
                await task
            await client.aclose()

    app = FastAPI(title="Agent Review Engine", version="0.2.0", lifespan=lifespan)

    def allowed(repository: str) -> bool:
        return bool(REPO_PATTERN.fullmatch(repository)) and (
            not settings.allowed_repos or repository.lower() in settings.allowed_repos)

    def admin_authorize(request: Request) -> None:
        candidate = request.headers.get("x-admin-token", "")
        if not settings.admin_metrics_token or not hmac.compare_digest(candidate, settings.admin_metrics_token):
            raise HTTPException(403, "Administrator token required")

    @app.get("/healthz")
    async def healthz():
        return {"status": "alive"}

    @app.get("/readyz")
    async def readyz():
        missing = settings.missing()
        if missing:
            return JSONResponse({"status": "not_ready", "missing": missing}, status_code=503)
        return {"status": "ready", "jev_enabled_default": settings.jev_enabled,
                "approval_mode_default": settings.approval_mode}

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(request: Request):
        length = request.headers.get("content-length", "")
        if length and (not length.isdecimal() or int(length) > settings.event_max_bytes):
            raise HTTPException(413, "Payload exceeds size limit")
        body = await request.body()
        if len(body) > settings.event_max_bytes:
            raise HTTPException(413, "Payload exceeds size limit")
        if not validate_signature(body, request.headers.get("x-hub-signature-256"),
                                  settings.github_webhook_secret):
            raise HTTPException(401, "Invalid webhook signature")
        delivery = request.headers.get("x-github-delivery", "")
        event = request.headers.get("x-github-event", "")
        if not delivery or len(delivery) > 150 or not event:
            raise HTTPException(400, "GitHub delivery and event headers are required")
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid JSON") from None
        repository = (payload.get("repository") or {}).get("full_name", "")
        if repository and not allowed(repository):
            return {"accepted": 0, "reason": "not enrolled"}
        jobs = route_event(event, payload, light_checks=settings.light_checks,
                           heavy_checks=settings.heavy_checks)
        count = store.enqueue(delivery, jobs, settings.pr_debounce_seconds)
        return {"accepted": count, "event": event}

    @app.post("/bridge/merge-group", status_code=202)
    async def bridge_merge_group(request: Request, group: BridgeGroup):
        """GHES 3.19 Actions bridge; does not trust installation id from callers."""
        if not settings.bridge_token or not hmac.compare_digest(
                request.headers.get("x-bridge-token", ""), settings.bridge_token):
            raise HTTPException(403, "Bridge token required")
        if not allowed(group.repository):
            raise HTTPException(403, "Repository is not enrolled")
        if not SHA_PATTERN.fullmatch(group.head_sha) or not SHA_PATTERN.fullmatch(group.base_sha):
            raise HTTPException(422, "SHA must be a 40-digit hex commit id")
        if not (group.head_ref.startswith("refs/heads/gh-readonly-queue/") or
                group.head_ref.startswith("gh-readonly-queue/")):
            raise HTTPException(422, "Only merge queue refs are allowed")
        installation_id = await request.app.state.github.repository_installation(group.repository)
        repo_head_sha = await request.app.state.github.ref_sha(
            installation_id, group.repository, group.head_ref)
        if repo_head_sha != group.head_sha:
            raise HTTPException(409, "Merge queue ref does not match head SHA")
        spec = JobSpec(kind="queue", repository=group.repository,
                       installation_id=installation_id, sha=group.head_sha,
                       base_sha=group.base_sha, head_ref=group.head_ref,
                       trigger="bridge.merge_group", force=True)
        count = store.enqueue(f"bridge:{group.repository}:{group.head_sha}:{group.base_sha}",
                              [spec], 0)
        return {"accepted": count}

    @app.get("/admin/metrics")
    async def metrics(request: Request, days: int = 30):
        admin_authorize(request)
        if days not in (1, 7, 30, 90):
            raise HTTPException(422, "days must be one of 1, 7, 30, 90")
        return store.metrics(days)

    @app.get("/admin/audit")
    async def audit(request: Request, repository: str = "", limit: int = 50):
        admin_authorize(request)
        if not 1 <= limit <= 200:
            raise HTTPException(422, "limit must be between 1 and 200")
        if repository and not allowed(repository):
            raise HTTPException(403, "Repository is invalid or not enrolled")
        return {"events": store.audit(repository or None, limit)}

    @app.get("/admin/summary")
    async def summary(request: Request):
        admin_authorize(request)
        return {"jobs": store.status_summary(), "recent": store.recent_jobs()}

    def safe_repo(owner: str, repo: str) -> str:
        name = f"{owner}/{repo}"
        if not allowed(name):
            raise HTTPException(403, "Repository is invalid or not enrolled")
        return name

    @app.get("/admin/repos/{owner}/{repo}")
    async def get_repo_settings(owner: str, repo: str, request: Request):
        admin_authorize(request)
        name = safe_repo(owner, repo)
        overrides = store.repo_settings(name)
        return {"repository": name, "overrides": overrides,
                "effective": {
                    "jev_enabled": (settings.jev_enabled if overrides["jev_enabled"] is None
                                    else overrides["jev_enabled"]),
                    "approval_mode": overrides["approval_mode"] or settings.approval_mode,
                    "auto_enqueue": (settings.auto_enqueue if overrides["auto_enqueue"] is None
                                     else overrides["auto_enqueue"]),
                    "llm_profile": overrides["llm_profile"] or "default"}}

    @app.patch("/admin/repos/{owner}/{repo}")
    async def update_repo_settings(owner: str, repo: str, update: RepoSettingsPatch,
                                   request: Request):
        admin_authorize(request)
        name = safe_repo(owner, repo)
        changed = update.model_dump(exclude_unset=True)
        if not changed:
            raise HTTPException(400, "No settings provided")
        if changed.get("llm_profile"):
            try:
                settings.resolve_llm_profile(changed["llm_profile"])
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise HTTPException(422, "Unknown or invalid LLM profile") from exc
        updated = store.update_repo_settings(name, changed)
        return {"repository": name, "overrides": updated,
                "note": "Applies to future events. Use /admin/recheck for existing PRs."}

    @app.post("/admin/recheck", status_code=202)
    async def recheck(request: Request, body: RecheckRequest):
        admin_authorize(request)
        if not allowed(body.repository):
            raise HTTPException(403, "Repository is invalid or not enrolled")
        github = request.app.state.github
        iid = await github.repository_installation(body.repository)
        pr = await github.pr(iid, body.repository, body.number)
        if pr.get("state") != "open":
            raise HTTPException(409, "Only open pull requests can be rechecked")
        sha = pr["head"]["sha"]
        specs = [JobSpec(kind="label", repository=body.repository, installation_id=iid,
                         sha=sha, pr_number=body.number, trigger="admin.recheck", force=True)]
        if not pr.get("draft"):
            specs.append(JobSpec(kind="pr", repository=body.repository, installation_id=iid,
                                 sha=sha, pr_number=body.number, trigger="admin.recheck", force=True))
        count = store.enqueue(f"admin:{uuid4().hex}", specs, 0)
        return {"accepted": count, "head_sha": sha, "draft": bool(pr.get("draft"))}

    app.state.store = store
    return app


app = create_app()
