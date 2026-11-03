from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from urllib.parse import urlsplit
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
    llm_profiles_json: str = "{}"
    llm_allowed_hosts: str = ""  # optional hostname allowlist for every LLM endpoint
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

    def available_llm_profiles(self) -> set[str]:
        profiles = json.loads(self.llm_profiles_json)
        if not isinstance(profiles, dict):
            raise ValueError("LLM_PROFILES_JSON must be a JSON object")
        if not all(re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,47}", n) for n in profiles):
            raise ValueError("Invalid LLM profile name")
        return {"default", *profiles}

    def resolve_llm_profile(self, name: str | None = None) -> dict[str, str | int]:
        """Profiles are provisioned server-side; repo admins can select names, not arbitrary URLs."""
        if not name or name == "default":
            values = {"base_url": self.llm_base_url, "model": self.llm_model,
                      "api_key": self.llm_api_key, "timeout_seconds": self.llm_timeout_seconds}
        else:
            profiles = json.loads(self.llm_profiles_json)
            if name not in self.available_llm_profiles() or name not in profiles:
                raise ValueError("Unknown LLM profile")
            spec = profiles[name]
            if not isinstance(spec, dict) or not all(k in spec for k in ("base_url", "model")):
                raise ValueError("Invalid LLM profile configuration")
            if any(key in spec for key in ("api_key", "token", "authorization", "headers")):
                raise ValueError("Store only secret environment variable names in LLM profiles")
            secret_env = spec.get("api_key_env", "")
            if secret_env and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", secret_env):
                raise ValueError("Invalid LLM secret environment variable name")
            resolved_key = os.environ.get(secret_env, "") if secret_env else ""
            if secret_env and not resolved_key:
                raise ValueError("Configured LLM API key environment variable is not set")
            values = {"base_url": spec["base_url"], "model": spec["model"],
                      "api_key": resolved_key,
                      "timeout_seconds": spec.get("timeout_seconds", self.llm_timeout_seconds)}
        url = urlsplit(str(values["base_url"]))
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("LLM base URL must be an HTTP(S) endpoint without credentials/query/fragment")
        allowed = {host.strip().casefold() for host in self.llm_allowed_hosts.split(",") if host.strip()}
        if allowed and url.hostname.casefold() not in allowed:
            raise ValueError("LLM endpoint hostname is not allowlisted")
        if not isinstance(values["model"], str) or not values["model"]:
            raise ValueError("LLM profile model is required")
        if not isinstance(values["timeout_seconds"], int) or not 5 <= values["timeout_seconds"] <= 600:
            raise ValueError("LLM timeout must be between 5 and 600 seconds")
        return values

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
