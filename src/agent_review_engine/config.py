from __future__ import annotations

from functools import lru_cache
from typing import Literal
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_api_url: str = "https://github.internal/api/v3"
    github_app_id: int = 0
    github_private_key_path: str = ""
    github_webhook_secret: str = ""
    github_bot_login: str = "agent-review-engine[bot]"
    database_url: str = "sqlite:///./review-engine.db"
    schema_auto_create: bool = True

    jev_enabled: bool = False
    jev_api_url: str = "https://api.typesafe.ai/v1/systemone"
    jev_api_key: str = ""
    jev_model: str = "jev-latest"
    jev_min_confidence: float = 0.7
    jev_send_description: bool = False

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: int = 120
    max_diff_chars: int = 40000
    max_files: int = 120

    light_ci_checks: str = "light-ci"
    heavy_ci_checks: str = "heavy-ci"
    ci_expected_app_id: int | None = None
    check_name: str = "agent-review-gate"
    approval_mode: Literal["report_only", "approve"] = "report_only"
    allow_medium_auto_approve: bool = True
    auto_enqueue: bool = False
    github_graphql_url: str = ""
    pr_debounce_seconds: int = 25
    poll_interval_seconds: int = 5
    ci_wait_timeout_seconds: int = 1800
    max_attempts: int = 6
    worker_enabled: bool = False
    bridge_token: str = ""
    repository_allowlist: str = ""
    event_max_bytes: int = 2_000_000
    admin_metrics_token: str = ""

    @property
    def light_checks(self) -> list[str]:
        return [c.strip() for c in self.light_ci_checks.split(",") if c.strip()]

    @property
    def heavy_checks(self) -> list[str]:
        return [c.strip() for c in self.heavy_ci_checks.split(",") if c.strip()]

    @property
    def graphql_url(self) -> str:
        if self.github_graphql_url:
            return self.github_graphql_url
        if not self.github_api_url.rstrip("/").endswith("/api/v3"):
            raise ValueError("Set GITHUB_GRAPHQL_URL for a nonstandard GitHub REST base")
        return self.github_api_url.rstrip("/").removesuffix("/v3") + "/graphql"

    @property
    def allowed_repos(self) -> set[str]:
        return {x.strip().lower() for x in self.repository_allowlist.split(",") if x.strip()}

    @field_validator("jev_min_confidence")
    @classmethod
    def confidence_bounds(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("jev_min_confidence must be in [0,1]")
        return value

    def missing(self) -> list[str]:
        required = {
            "GITHUB_APP_ID": self.github_app_id,
            "GITHUB_PRIVATE_KEY_PATH": self.github_private_key_path,
            "GITHUB_WEBHOOK_SECRET": self.github_webhook_secret,
            "LLM_BASE_URL": self.llm_base_url,
            "LLM_MODEL": self.llm_model,
        }
        if not self.light_checks or not self.heavy_checks:
            return [*([k for k, v in required.items() if not v]), "CI_CHECKS"]
        issues = [k for k, v in required.items() if not v]
        if self.check_name in (*self.light_checks, *self.heavy_checks):
            issues.append("CHECK_NAME (must be distinct)")
        if self.approval_mode == "approve" and self.ci_expected_app_id is None:
            issues.append("CI_EXPECTED_APP_ID (required for active approval)")
        return issues


@lru_cache
def get_settings() -> Settings:
    return Settings()
