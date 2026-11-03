from __future__ import annotations

import asyncio
import logging
import time

import httpx
from .config import Settings
from .github import GitHubAPI
from .models import JobSpec, ReviewVerdict
from .policy import fallback_labels, allowed_to_approve, requires_human
from .providers import JevClassifier, LLMReviewer
from .store import Store

log = logging.getLogger(__name__)


class RetryLater(Exception):
    def __init__(self, delay: int, reason: str, *, count: bool = False):
        super().__init__(reason)
        self.delay, self.reason, self.count = delay, reason, count


class ReviewEngine:
    def __init__(self, settings: Settings, store: Store, github: GitHubAPI,
                 classifier: JevClassifier, reviewer: LLMReviewer):
        self.settings = settings
        self.store = store
        self.github = github
        self.classifier = classifier
        self.reviewer = reviewer

    async def execute(self, spec: JobSpec, state: dict) -> tuple[str, dict]:
        if spec.kind == "label":
            return await self._label(spec, state)
        if spec.kind == "pr":
            return await self._pr(spec, state)
        return await self._queue(spec, state)

    async def _label(self, spec: JobSpec, state: dict) -> tuple[str, dict]:
        pr = await self.github.pr(spec.installation_id, spec.repository, spec.pr_number)
        if pr.get("state") != "open" or pr["head"]["sha"] != spec.sha:
            return "stale", state
        files = await self.github.files(spec.installation_id, spec.repository, spec.pr_number)
        paths = [f["filename"] for f in files]
        baseline = fallback_labels(paths)
        repo_settings = self.store.repo_settings(spec.repository)
        jev_enabled = (self.settings.jev_enabled if repo_settings["jev_enabled"] is None
                       else repo_settings["jev_enabled"])
        labels = baseline
        state["jev_status"] = "disabled"
        if jev_enabled:
            try:
                labels = await self.classifier.classify(
                    title=pr.get("title", ""), description=pr.get("body") or "",
                    files=files, enabled=True)
                state["jev_status"] = "ok"
                state["jev_confidence"] = labels.confidence
            except (httpx.HTTPError, KeyError, ValueError, RuntimeError, TypeError) as exc:
                log.warning("Jev classification unavailable for %s #%s: %s",
                            spec.repository, spec.pr_number, type(exc).__name__)
                state["jev_status"] = "degraded"
                state["jev_error"] = type(exc).__name__
        # Jev provides labels, NOT a security/approval verdict. Central deterministic
        # policy independently determines what may auto-approve.
        risk = baseline.risk  # central policy classification, never downgraded by Jev
        area = labels.area
        result = [f"risk:{risk}", f"area:{area}"]
        if state["jev_status"] == "ok":
            result.append(f"jev:risk-{labels.risk}")
            state["jev_risk"] = labels.risk
        if risk == "high":
            result.append("review:human-required")
        if state["jev_status"] == "degraded":
            result.append("review:jev-degraded")
        # Important: never wipe unrelated human-managed labels.
        await self.github.replace_review_labels(spec.installation_id, spec.repository,
                                                spec.pr_number, result)
        state["labels"] = result
        state["risk"] = risk
        state["area"] = area
        return "done", state

    async def _check(self, spec: JobSpec, state: dict, status: str,
                     title: str, summary: str) -> None:
        check_id = await self.github.check(spec.installation_id, spec.repository, spec.sha,
                                           status, title, summary,
                                           check_id=state.get("check_id"))
        state["check_id"] = check_id

    async def _ci(self, spec: JobSpec, state: dict, *, phase: str) -> bool:
        checks = self.settings.light_checks if phase == "pr" else self.settings.heavy_checks
        if self.settings.check_name in checks:
            await self._check(spec, state, "failure", "Invalid CI configuration",
                              "Required CI check must not be the review gate itself")
            return False
        ci_state, summary = await self.github.ci_state(
            spec.installation_id, spec.repository, spec.sha, checks)
        if ci_state == "success":
            state["ci_summary"] = summary
            return True
        if ci_state == "failure":
            await self._check(spec, state, "failure", "CI failed", summary)
            return False
        first = state.setdefault("ci_wait_since", time.time())
        if time.time() - first > self.settings.ci_wait_timeout_seconds:
            await self._check(spec, state, "failure", "CI timed out", summary)
            return False
        await self._check(spec, state, "in_progress", "Waiting for CI", summary)
        raise RetryLater(20, summary)

    @staticmethod
    def _format_result(verdict: ReviewVerdict, risk: str, usage: dict) -> str:
        content = [f"AI review: **{verdict.verdict}** | risk: **{risk}**",
                   f"\n{verdict.summary}"]
        for f in verdict.findings[:20]:
            content.append(f"\n- **{f.severity}** `{f.path}`: {f.explanation} (Evidence: {f.evidence})")
        if usage:
            content.append(f"\nTokens: {usage.get('prompt_tokens', 0)} input / "
                           f"{usage.get('completion_tokens', 0)} output")
        return "\n".join(content)

    async def _pr(self, spec: JobSpec, state: dict) -> tuple[str, dict]:
        iid, repo, number = spec.installation_id, spec.repository, spec.pr_number
        pr = await self.github.pr(iid, repo, number)
        if pr.get("state") != "open" or pr["head"]["sha"] != spec.sha:
            return "stale", state
        if pr.get("draft"):
            return "draft", state
        files = await self.github.files(iid, repo, number)
        # Classification labels are advisory and may be unavailable. Never let an
        # optional Jev failure block LLM review, or let a forged label lower risk.
        risk = fallback_labels([f["filename"] for f in files]).risk
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
        # Fork PRs can carry untrusted code or workflow edits. Require a human.
        if head_repo and head_repo.lower() != repo.lower():
            risk = "high"
            state["fork_requires_human"] = True
        state["risk"] = risk
        repo_settings = self.store.repo_settings(repo)
        mode = repo_settings["approval_mode"] or self.settings.approval_mode
        if not await self._ci(spec, state, phase="pr"):
            return "blocked", state
        if "verdict" not in state:
            try:
                verdict, usage = await self.reviewer.review(
                    title=pr.get("title", ""), description=pr.get("body") or "",
                    files=files, ci_summary=state["ci_summary"], phase="pr",
                    **({"profile_name": repo_settings["llm_profile"]} if repo_settings.get("llm_profile") else {}))
                state["llm_profile"] = repo_settings.get("llm_profile") or "default"
                state["verdict"] = verdict.model_dump()
                state["usage"] = usage
            except (ValueError, KeyError) as exc:
                await self._check(spec, state, "action_required", "Unreviewable patch",
                                  f"Review could not safely evaluate diff: {type(exc).__name__}: {str(exc)[:500]}")
                return "blocked", state
        verdict = ReviewVerdict.model_validate(state["verdict"])
        detail = self._format_result(verdict, risk, state.get("usage", {}))
        # Re-read the PR before modifying GitHub to avoid approving a superseded commit.
        fresh = await self.github.pr(iid, repo, number)
        if fresh.get("draft") or fresh.get("state") != "open" or fresh["head"]["sha"] != spec.sha:
            return "stale", state
        if verdict.verdict != "approve":
            if not state.get("review_posted") and mode == "approve":
                await self.github.submit_review(iid, repo, number, spec.sha, "COMMENT", detail)
                state["review_posted"] = True
            await self._check(spec, state, "action_required", "AI review needs changes / human review", detail)
            return "blocked", state

        if requires_human(risk, self.settings.allow_medium_auto_approve):
            author = (fresh.get("user") or {}).get("login", "")
            if not await self.github.human_approval(iid, repo, number, spec.sha, author):
                if not state.get("review_posted") and mode == "approve":
                    await self.github.submit_review(iid, repo, number, spec.sha, "COMMENT",
                                                    detail + "\n\nHuman approval required by central policy.")
                    state["review_posted"] = True
                await self._check(spec, state, "action_required", "Human approval required", detail)
                return "blocked", state
            if mode != "approve":
                await self._check(spec, state, "action_required", "Report-only mode", detail)
                return "done", state
            await self._check(spec, state, "success", "Human approval and AI check satisfied", detail)
            await self._maybe_enqueue(spec, state, fresh)
            if state.get("enqueue_status") == "pending":
                await self._check(spec, state, "success", "Review passed; queue entry pending",
                                  detail + "\n\nQueue entry pending: check other GHES branch requirements.")
            return "done", state

        eligible, reason = allowed_to_approve(
            verdict, risk, allow_medium=self.settings.allow_medium_auto_approve)
        if not eligible:
            await self._check(spec, state, "action_required", reason, detail)
            return "blocked", state
        if mode != "approve":
            await self._check(spec, state, "action_required", "Report-only mode", detail +
                              "\n\nNo approval was submitted. Disable required gate during shadow rollout.")
            return "done", state
        if not state.get("review_posted"):
            # Idempotent retry: if the write succeeded but our process crashed,
            # avoid duplicating the approval for this head SHA.
            existing = await self.github.reviews(iid, repo, number)
            bot = self.settings.github_bot_login.casefold()
            already = any((r.get("user") or {}).get("login", "").casefold() == bot
                          and r.get("state") == "APPROVED" and r.get("commit_id") == spec.sha
                          for r in existing)
            if not already:
                await self.github.submit_review(iid, repo, number, spec.sha, "APPROVE", detail)
            state["review_posted"] = True
        await self._check(spec, state, "success", "LLM review approved", detail)
        await self._maybe_enqueue(spec, state, fresh)
        if state.get("enqueue_status") == "pending":
            await self._check(spec, state, "success", "Review passed; queue entry pending",
                              detail + "\n\nQueue entry pending: check other GHES branch requirements.")
        return "done", state

    async def _maybe_enqueue(self, spec: JobSpec, state: dict, pr: dict) -> None:
        repo_settings = self.store.repo_settings(spec.repository)
        enabled = (self.settings.auto_enqueue if repo_settings["auto_enqueue"] is None
                   else repo_settings["auto_enqueue"])
        if not enabled or state.get("enqueue_status") == "enqueued":
            return
        if not pr.get("node_id"):
            state["enqueue_status"] = "pending"
            state["enqueue_error"] = "GitHub PR node_id not available"
            return
        try:
            await self.github.enqueue_pull_request(spec.installation_id, pr["node_id"], spec.sha)
            state["enqueue_status"] = "enqueued"
            state.pop("enqueue_error", None)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            # Required checks can succeed while GitHub still waits on other branch rules.
            # Never bypass GitHub policy or attempt a direct merge.
            state["enqueue_status"] = "pending"
            state["enqueue_error"] = type(exc).__name__
            log.warning("Queue admission pending: %s #%s: %s", spec.repository,
                        spec.pr_number, type(exc).__name__)

    async def _queue(self, spec: JobSpec, state: dict) -> tuple[str, dict]:
        iid, repo = spec.installation_id, spec.repository
        if spec.head_ref:
            try:
                actual_sha = await self.github.ref_sha(iid, repo, spec.head_ref)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return "stale", state  # queue group was already removed
                raise
            if actual_sha != spec.sha:
                return "stale", state
        base = spec.base_sha
        if not base and spec.base_ref:
            base = await self.github.ref_sha(iid, repo, spec.base_ref)
        # Never infer a merge-group base from a synthetic merge commit's parent.
        # An ambiguous comparison must fail closed instead of silently approving.
        if not base:
            await self._check(spec, state, "action_required", "Merge group has no base",
                              "Cannot establish a reliable merge-group diff")
            return "blocked", state
        if not await self._ci(spec, state, phase="queue"):
            return "blocked", state
        if "verdict" not in state:
            try:
                files = await self.github.compare_files(iid, repo, base, spec.sha)
                repo_settings = self.store.repo_settings(repo)
                verdict, usage = await self.reviewer.review(
                    title="Merge Queue integration validation", description=f"Base {base} to {spec.sha}",
                    files=files, ci_summary=state["ci_summary"], phase="merge_group",
                    **({"profile_name": repo_settings["llm_profile"]} if repo_settings.get("llm_profile") else {}))
                state["llm_profile"] = repo_settings.get("llm_profile") or "default"
                state["verdict"] = verdict.model_dump()
                state["usage"] = usage
            except (ValueError, KeyError) as exc:
                await self._check(spec, state, "action_required", "Merge group cannot be reviewed",
                                  f"Insufficient merge-group diff: {type(exc).__name__}: {str(exc)[:500]}")
                return "blocked", state
        verdict = ReviewVerdict.model_validate(state["verdict"])
        mode = self.store.repo_settings(repo)["approval_mode"] or self.settings.approval_mode
        detail = self._format_result(verdict, "integration", state.get("usage", {}))
        if verdict.verdict != "approve" or any(f.severity in {"critical", "high"} for f in verdict.findings):
            await self._check(spec, state, "failure", "Merge Queue integration review failed", detail)
            return "blocked", state
        if mode != "approve":
            await self._check(spec, state, "action_required", "Report-only mode", detail)
            return "done", state
        await self._check(spec, state, "success", "Merge Queue integration review passed", detail)
        return "done", state

    async def worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            claimed = self.store.claim()
            if claimed is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.settings.poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
                continue
            key, generation, spec, state = claimed
            try:
                status, state = await self.execute(spec, state)
                self.store.complete(key, generation, state, status)
            except RetryLater as exc:
                self.store.requeue(key, generation, state, exc.delay, exc.reason, count=exc.count)
            except Exception as exc:
                # Keep raw PR content, API keys and full HTTP bodies out of logs.
                state["error_attempts"] = state.get("error_attempts", 0) + 1
                attempts = self.store.requeue(key, generation, state, 2 ** min(6, state["error_attempts"]),
                                              type(exc).__name__, count=True)
                log.exception("Review job failed: kind=%s repo=%s attempt=%s", spec.kind,
                              spec.repository, attempts)
                if attempts >= self.settings.max_attempts:
                    try:
                        if spec.kind != "label":
                            await self._check(spec, state, "failure", "Review service error",
                                              f"Policy failed closed after {attempts} attempts; operator action required")
                    except Exception:
                        log.exception("Could not publish terminal failure check")
                    self.store.complete(key, generation, state, "blocked", type(exc).__name__)
