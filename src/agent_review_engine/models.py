from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Finding(BaseModel):
    path: str = ""
    severity: Literal["critical", "high", "medium", "low"]
    explanation: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class ReviewVerdict(BaseModel):
    verdict: Literal["approve", "request_changes", "needs_human"]
    summary: str = Field(min_length=1)
    findings: list[Finding] = Field(default_factory=list, max_length=30)


class Labels(BaseModel):
    risk: Literal["low", "medium", "high"]
    area: Literal["frontend", "backend", "infra", "docs", "mixed"]
    confidence: float = Field(ge=0, le=1)


class JobSpec(BaseModel):
    kind: Literal["label", "pr", "queue"]
    repository: str
    installation_id: int
    sha: str
    pr_number: int | None = None
    base_sha: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    trigger: str
    force: bool = False

    @property
    def key(self) -> str:
        # A force re-evaluation reuses the stored record for this phase/SHA.
        prefix = self.pr_number if self.pr_number is not None else "group"
        return f"{self.repository}:{prefix}:{self.kind}:{self.sha}"
