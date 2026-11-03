from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
import httpx
import jwt

from .config import Settings


class GitHubAPI:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=35)
        self._tokens: dict[int, tuple[str, float]] = {}

    async def close(self):
        await self.client.aclose()

    def _app_jwt(self) -> str:
        private_key = Path(self.settings.github_private_key_path).read_text(encoding="utf-8")
        now = int(time.time())
        return jwt.encode({"iat": now - 30, "exp": now + 540,
                           "iss": str(self.settings.github_app_id)}, private_key, algorithm="RS256")

    async def repository_installation(self, repository: str) -> int:
        url = self.settings.github_api_url.rstrip("/") + f"{self.path(repository)}/installation"
        response = await self.client.get(url, headers={"Authorization": f"Bearer {self._app_jwt()}",
                                                      "Accept": "application/vnd.github+json"})
        response.raise_for_status()
        return int(response.json()["id"])

    async def token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached[1] > time.time() + 120:
            return cached[0]
        assertion = self._app_jwt()
        url = self.settings.github_api_url.rstrip("/") + f"/app/installations/{installation_id}/access_tokens"
        response = await self.client.post(url, headers={"Authorization": f"Bearer {assertion}",
                                                       "Accept": "application/vnd.github+json"})
        response.raise_for_status()
        result = response.json()
        expiry = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00")).timestamp()
        self._tokens[installation_id] = (result["token"], expiry)
        return result["token"]

    async def request(self, installation_id: int, method: str, resource: str, **kwargs):
        token = await self.token(installation_id)
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                   "X-GitHub-Api-Version": "2022-11-28"}
        headers.update(kwargs.pop("headers", {}))
        result = await self.client.request(method, self.settings.github_api_url.rstrip("/") + resource,
                                           headers=headers, **kwargs)
        result.raise_for_status()
        if result.status_code == 204:
            return None
        return result.json()

    @staticmethod
    def path(repository: str) -> str:
        owner, repo = repository.split("/", 1)
        return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


    async def graphql(self, iid: int, query: str, variables: dict) -> dict:
        token = await self.token(iid)
        response = await self.client.post(
            self.settings.graphql_url, headers={"Authorization": f"Bearer {token}",
                                               "Accept": "application/vnd.github+json"},
            json={"query": query, "variables": variables})
        response.raise_for_status()
        result = response.json()
        if result.get("errors"):
            # Do not concatenate untrusted error bodies or credentials into operator logs.
            raise ValueError("GraphQL queue operation was rejected by GitHub")
        return result["data"]

    async def enqueue_pull_request(self, iid: int, pr_node_id: str, sha: str) -> None:
        query = """mutation($input: EnqueuePullRequestInput!) {
          enqueuePullRequest(input: $input) { mergeQueueEntry { id } }
        }"""
        await self.graphql(iid, query, {"input": {"pullRequestId": pr_node_id,
                                                  "expectedHeadOid": sha, "jump": False}})

    async def pr(self, iid: int, repo: str, number: int) -> dict:
        return await self.request(iid, "GET", f"{self.path(repo)}/pulls/{number}")

    async def files(self, iid: int, repo: str, number: int) -> list[dict]:
        base = f"{self.path(repo)}/pulls/{number}/files"
        items: list[dict] = []
        for page in range(1, 5):
            batch = await self.request(iid, "GET", base, params={"per_page": 100, "page": page})
            items.extend(batch)
            if len(batch) < 100:
                break
        if len(items) > self.settings.max_files:
            raise ValueError(f"Too many changed files: {len(items)} > {self.settings.max_files}")
        return items

    async def compare_files(self, iid: int, repo: str, base: str, head: str) -> list[dict]:
        result = await self.request(iid, "GET", f"{self.path(repo)}/compare/{quote(base)}...{quote(head)}")
        if result.get("total_commits", 0) > 250 or len(result.get("files", [])) >= 300:
            raise ValueError("Merge queue compare too large to review completely")
        files = result.get("files") or []
        if len(files) > self.settings.max_files:
            raise ValueError("Merge queue diff exceeds max_files")
        return files

    async def ref_sha(self, iid: int, repo: str, ref: str) -> str:
        ref = ref.removeprefix("refs/")
        if not ref.startswith("heads/") or ".." in ref or "?" in ref or "#" in ref:
            raise ValueError("Unsafe or invalid base ref")
        result = await self.request(iid, "GET", f"{self.path(repo)}/git/ref/{quote(ref, safe='/')}")
        return result["object"]["sha"]

    async def commit(self, iid: int, repo: str, sha: str) -> dict:
        return await self.request(iid, "GET", f"{self.path(repo)}/commits/{sha}")

    async def label_names(self, iid: int, repo: str, number: int) -> list[str]:
        names: list[str] = []
        for page in range(1, 5):
            batch = await self.request(iid, "GET", f"{self.path(repo)}/issues/{number}/labels",
                                       params={"per_page": 100, "page": page})
            names.extend(entry["name"] for entry in batch)
            if len(batch) < 100:
                break
        return names

    async def replace_review_labels(self, iid: int, repo: str, number: int,
                                    names: list[str]):
        existing = await self.label_names(iid, repo, number)
        owned = ("risk:", "area:", "jev:risk-", "review:human-required", "review:jev-degraded")
        to_remove = [name for name in existing if name.startswith(owned[:2])
                     or name.startswith(owned[2]) or name in owned[3:]]
        for label in to_remove:
            if label not in names:
                await self.request(iid, "DELETE", f"{self.path(repo)}/issues/{number}/labels/{quote(label, safe='')}")
        to_add = [name for name in names if name not in existing]
        if to_add:
            colors = {"risk:low": "198754", "risk:medium": "b7791f", "risk:high": "b42318",
                      "area:frontend": "4473a8", "area:backend": "56657b",
                      "area:infra": "7255aa", "area:docs": "687782",
                      "area:mixed": "455469", "review:human-required": "b42318",
                      "review:jev-degraded": "bc841b", "jev:risk-low": "4b9b6a",
                      "jev:risk-medium": "b7791f", "jev:risk-high": "b42318"}
            # GitHub cannot add labels which have not been created in the repository.
            for label in to_add:
                if label not in colors:
                    continue
                try:
                    await self.request(iid, "POST", f"{self.path(repo)}/labels", json={
                        "name": label, "color": colors[label], "description": "Managed by Agent Review Engine"})
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 422:  # Already exists/race (GitHub 422)
                        raise
            await self.request(iid, "POST", f"{self.path(repo)}/issues/{number}/labels",
                               json={"labels": to_add})

    async def checks(self, iid: int, repo: str, sha: str) -> list[dict]:
        result = await self.request(iid, "GET", f"{self.path(repo)}/commits/{sha}/check-runs",
                                    params={"per_page": 100, "filter": "latest"})
        if result.get("total_count", 0) > 100:
            raise ValueError("Too many check runs to evaluate reliably")
        return result.get("check_runs", [])

    async def ci_state(self, iid: int, repo: str, sha: str, required: list[str]):
        """Only exact-name successful checks count. Missing/neutral/skipped never pass."""
        if not required:
            return "failure", "No required CI check configured"
        runs = await self.checks(iid, repo, sha)
        status_data = await self.request(iid, "GET", f"{self.path(repo)}/commits/{sha}/status")
        statuses = status_data.get("statuses", [])
        pending = []
        failures = []
        for name in required:
            matching = [run for run in runs if run.get("name") == name and
                        (self.settings.ci_expected_app_id is None or
                         (run.get("app") or {}).get("id") == self.settings.ci_expected_app_id)]
            if matching:
                latest = max(matching, key=lambda r: r.get("id", 0))
                conclusion = latest.get("conclusion")
                if latest.get("status") != "completed":
                    pending.append(name)
                elif conclusion != "success":
                    failures.append(f"{name}={conclusion}")
                continue
            if self.settings.ci_expected_app_id is None:
                candidates = [s for s in statuses if s.get("context") == name]
                if candidates:
                    latest = candidates[0]  # API returns newest first
                    state = latest.get("state")
                    if state == "success":
                        continue
                    if state in ("error", "failure"):
                        failures.append(f"{name}={state}")
                    else:
                        pending.append(name)
                    continue
            pending.append(name)
        if failures:
            return "failure", ", ".join(failures)
        if pending:
            return "pending", "Waiting for: " + ", ".join(pending)
        return "success", "All required CI checks succeeded"

    async def check(self, iid: int, repo: str, sha: str, status: str, title: str,
                    summary: str, check_id: int | None = None) -> int:
        payload = {"name": self.settings.check_name, "head_sha": sha,
                   "status": "in_progress" if status == "in_progress" else "completed",
                   "output": {"title": title[:255], "summary": summary[:60000]}}
        if status != "in_progress":
            payload["conclusion"] = status
        if check_id:
            payload.pop("name")
            payload.pop("head_sha")
            result = await self.request(iid, "PATCH", f"{self.path(repo)}/check-runs/{check_id}",
                                        json=payload)
        else:
            result = await self.request(iid, "POST", f"{self.path(repo)}/check-runs", json=payload)
        return result["id"]

    async def submit_review(self, iid: int, repo: str, number: int, sha: str,
                            event: str, body: str):
        return await self.request(iid, "POST", f"{self.path(repo)}/pulls/{number}/reviews",
                                  json={"event": event, "commit_id": sha, "body": body[:60000]})

    async def reviews(self, iid: int, repo: str, number: int) -> list[dict]:
        out = []
        for page in range(1, 4):
            batch = await self.request(iid, "GET", f"{self.path(repo)}/pulls/{number}/reviews",
                                       params={"per_page": 100, "page": page})
            out.extend(batch)
            if len(batch) < 100:
                break
        if len(out) >= 300:
            raise ValueError("Too many reviews to evaluate reliably")
        return out

    async def human_approval(self, iid: int, repo: str, number: int,
                             sha: str, author: str) -> bool:
        reviews = await self.reviews(iid, repo, number)
        last_by_user = {}
        for review in reviews:
            user = (review.get("user") or {}).get("login", "")
            if user:
                last_by_user[user] = review
        for login, review in last_by_user.items():
            if login == author or login.endswith("[bot]"):
                continue
            if review.get("state") != "APPROVED" or review.get("commit_id") != sha:
                continue
            permission = await self.request(iid, "GET", f"{self.path(repo)}/collaborators/{quote(login)}/permission")
            if permission.get("permission") in {"admin", "maintain", "write"}:
                return True
        return False
