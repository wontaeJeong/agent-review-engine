from __future__ import annotations

from .models import JobSpec


def route_event(event: str, payload: dict, *, light_checks: list[str] | None = None,
                heavy_checks: list[str] | None = None) -> list[JobSpec]:
    """Map webhook events to idempotent, SHA-scoped work. Ignore label feedback loops."""
    action = payload.get("action", "")
    repo = payload.get("repository") or {}
    repository = repo.get("full_name")
    installation = (payload.get("installation") or {}).get("id")
    if not repository or not isinstance(installation, int) or installation <= 0:
        return []

    def job(kind: str, sha: str, *, number: int | None = None,
            base_sha: str | None = None, base_ref: str | None = None,
            head_ref: str | None = None, force: bool = False) -> JobSpec:
        return JobSpec(kind=kind, repository=repository, installation_id=installation,
                       sha=sha, pr_number=number, base_sha=base_sha, base_ref=base_ref, head_ref=head_ref,
                       trigger=f"{event}.{action or 'event'}", force=force)

    if event == "pull_request":
        pr = payload.get("pull_request") or {}
        sha = (pr.get("head") or {}).get("sha")
        number = pr.get("number") or payload.get("number")
        if not sha or not number or pr.get("state") != "open":
            return []
        draft = bool(pr.get("draft"))
        if action in {"opened", "reopened", "synchronize"}:
            jobs = [job("label", sha, number=number)]
            if not draft:
                jobs.append(job("pr", sha, number=number, force=(action == "reopened")))
            return jobs
        if action == "ready_for_review":
            return [job("pr", sha, number=number, force=True)] if not draft else []
        if action == "edited" and {"title", "body", "base"}.intersection(payload.get("changes") or {}):
            jobs = [job("label", sha, number=number, force=True)]
            if not draft:
                jobs.append(job("pr", sha, number=number, force=True))
            return jobs
        return []

    if event == "pull_request_review" and action in {"submitted", "dismissed"}:
        pr = payload.get("pull_request") or {}
        sha = (pr.get("head") or {}).get("sha")
        if not sha or pr.get("state") != "open" or pr.get("draft"):
            return []
        # To avoid a feedback loop, the app's own review is never an input trigger.
        reviewer = (payload.get("review") or {}).get("user") or {}
        if reviewer.get("type") == "Bot" or reviewer.get("login", "").endswith("[bot]"):
            return []
        return [job("pr", sha, number=pr.get("number") or payload.get("number"), force=True)]

    if event == "check_run" and action in {"completed", "rerequested"}:
        run = payload.get("check_run") or {}
        name, sha = run.get("name"), run.get("head_sha")
        if not sha:
            return []
        if name in (light_checks or []):
            jobs = []
            for pr in run.get("pull_requests") or []:
                number = pr.get("number")
                pr_sha = (pr.get("head") or {}).get("sha") or sha
                if number and pr_sha == sha:
                    jobs.append(job("pr", sha, number=number, force=True))
            return jobs
        if name in (heavy_checks or []):
            branch = run.get("check_suite", {}).get("head_branch") or run.get("head_branch") or ""
            if branch.startswith("gh-readonly-queue/") and "/pr-" in branch:
                base_name = branch.removeprefix("gh-readonly-queue/").rsplit("/pr-", 1)[0]
                if base_name:
                    return [job("queue", sha, base_ref=f"heads/{base_name}",
                                head_ref=f"heads/{branch}", force=True)]
        return []

    if event == "push":
        ref = payload.get("ref", "")
        sha = payload.get("after")
        if ref.startswith("refs/heads/gh-readonly-queue/") and sha and set(sha) != {"0"}:
            # GHES 3.19 supports queue branch push webhook for external CI providers.
            # Parse the target branch from gh-readonly-queue/<base>/pr-<id>.
            tail = ref.removeprefix("refs/heads/gh-readonly-queue/")
            if "/pr-" not in tail:
                return []
            base_name = tail.rsplit("/pr-", 1)[0]
            if not base_name:
                return []
            return [job("queue", sha, base_ref=f"heads/{base_name}",
                        head_ref=f"heads/gh-readonly-queue/{tail}")]
        return []

    if event == "merge_group" and action == "checks_requested":
        group = payload.get("merge_group") or {}
        sha = group.get("head_sha")
        if sha:
            return [job("queue", sha, base_sha=group.get("base_sha"),
                        base_ref=group.get("base_ref"), head_ref=group.get("head_ref"))]
    return []
