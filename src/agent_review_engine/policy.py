from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath
from .models import Labels, ReviewVerdict

RANK = {"low": 0, "medium": 1, "high": 2}

# Policy lives in the central service; an untrusted PR cannot rewrite these rules.
HIGH_RISK_SEGMENTS = {
    "auth", "authentication", "authorization", "security", "secrets", "migrations",
    "terraform", "k8s", "kubernetes", "helm", "rbac", "admission", "policies",
}
HIGH_RISK_NAMES = {"dockerfile", "codeowners", "dependabot.yml", "pyproject.toml",
                   "package.json", "go.mod", "go.sum", "cargo.lock"}
MEDIUM_RISK_SEGMENTS = {"api", "database", "db", "backend", "infra", "deployment"}


def path_risk(paths: Iterable[str]) -> str:
    risk = "low"
    for path in paths:
        lower = path.lower().lstrip("/")
        parts = PurePosixPath(lower).parts
        if (
            ".github/workflows/" in lower
            or ".github/actions/" in lower
            or ".github/codeowners" in lower
            or "requirements" in lower and lower.endswith(".txt")
            or lower.endswith(".lock")
            or parts[-1] in HIGH_RISK_NAMES
            or HIGH_RISK_SEGMENTS.intersection(parts)
        ):
            return "high"
        if MEDIUM_RISK_SEGMENTS.intersection(parts) or lower.endswith(
                (".py", ".go", ".js", ".ts", ".sql", ".java", ".rs", ".sh")):
            risk = "medium"
    return risk


def effective_risk(classification: str | None, paths: Iterable[str]) -> str:
    mandatory = path_risk(paths)
    if classification not in RANK:
        return "high"  # missing classifier = fail closed
    return max((classification, mandatory), key=lambda v: RANK[v])


def allowed_to_approve(
    verdict: ReviewVerdict,
    risk: str,
    *,
    allow_medium: bool = True,
    has_human_approval: bool = False,
) -> tuple[bool, str]:
    if verdict.verdict != "approve":
        return False, "LLM did not approve"
    if any(f.severity in {"critical", "high", "medium"} for f in verdict.findings):
        return False, "Blocking findings exist"
    if risk == "high":
        return False, "High-risk changes require authorized human approval"
    if risk == "medium" and not allow_medium:
        return False, "Medium-risk auto approval disabled"
    return True, "Eligible for automated approval"


def requires_human(risk: str, allow_medium: bool = True) -> bool:
    return risk == "high" or (risk == "medium" and not allow_medium)


def fallback_labels(paths: Iterable[str]) -> Labels:
    """Deterministic metadata fallback; Jev is NOT an authorization authority."""
    names = list(paths)
    areas = set()
    for path in names:
        segments = PurePosixPath(path.lower()).parts
        if path.lower().endswith((".md", ".rst", ".txt")) or "docs" in segments:
            areas.add("docs")
        elif set(segments) & {"helm", "k8s", "kubernetes", "infra", "terraform", ".github"}:
            areas.add("infra")
        elif set(segments) & {"web", "frontend", "ui", "components", "client"} or path.lower().endswith((".tsx", ".jsx", ".css")):
            areas.add("frontend")
        elif set(segments) & {"src", "api", "backend", "server", "services"} or path.lower().endswith((".py", ".go")):
            areas.add("backend")
        else:
            areas.add("mixed")
    area = next(iter(areas)) if len(areas) == 1 else "mixed"
    return Labels(risk=path_risk(names), area=area, confidence=1.0)
