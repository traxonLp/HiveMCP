"""Runtime configuration.

Every setting comes from an ``HIVE_*`` environment variable so the container is
configurable through a Kubernetes ConfigMap/Secret without a config file on the PVC.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Settings where an empty value must mean "not configured" rather than "configured as
# an empty string". A .env written from the template carries lines like
# ``HIVE_AUTH_TOKEN=``, and without this normalisation that reads as an empty string:
# not None, therefore truthy to an `is not None` check, therefore auth switches on with
# an empty token and rejects every request with 401.
_OPTIONAL_SECRETS = (
    "auth_token",
    "owui_url",
    "owui_api_key",
    "llm_fallback_model",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HIVE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator(*_OPTIONAL_SECRETS, mode="before")
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("signing_key", mode="before")
    @classmethod
    def _generate_when_blank(cls, value: object) -> object:
        # An empty signing key would still sign and verify, so nothing would look
        # broken, but every deployment that left the line empty would share it.
        if isinstance(value, str) and not value.strip():
            return secrets.token_urlsafe(32)
        return value

    # --- Identity / networking -------------------------------------------------
    public_url: str = Field(
        default="http://localhost:8080",
        description="Externally reachable base URL. Used to build signed /ui links "
        "that the sandboxed iframe loads, so it must be the URL the *browser* sees.",
    )
    log_level: str = "INFO"
    environment: Literal["dev", "prod"] = "dev"

    # --- Auth ------------------------------------------------------------------
    auth_token: str | None = Field(
        default=None,
        description="Shared bearer token between OpenWebUI and HiveMCP. "
        "When unset in dev, auth is disabled; in prod a missing token is a hard error.",
    )
    signing_key: str = Field(
        default_factory=lambda: secrets.token_urlsafe(32),
        description="HMAC key for signed /ui URLs. MUST be set explicitly when running "
        "more than one replica, otherwise each pod signs with a different key and "
        "links break on any request that lands on another pod.",
    )
    ui_token_ttl_seconds: int = 900

    # --- Storage ---------------------------------------------------------------
    data_dir: Path = Field(default=Path("/data"))
    tmp_ttl_minutes: int = 60
    max_upload_mb: int = 50
    max_render_concurrency: int = Field(
        default=4,
        description="Semaphore over rendering jobs. Rendering is memory-spiky; "
        "unbounded concurrency is the most likely cause of an OOMKill.",
    )
    mcp_host: str = Field(
        default="0.0.0.0",  # noqa: S104 - the container listens on all interfaces
        description="Host the MCP transport believes it is bound to. The SDK uses this "
        "only to decide whether to auto-enable DNS rebinding protection; leaving it at "
        "the SDK's 127.0.0.1 default makes a containerised server reject requests that "
        "arrive with a Host header like 'hivemcp:8080'.",
    )

    # --- OpenWebUI -------------------------------------------------------------
    owui_url: str | None = None
    owui_api_key: str | None = None
    owui_timeout_seconds: float = 30.0

    # --- Optional LLM expansion (hybrid mode) ----------------------------------
    # There is no separate LLM endpoint: expansion calls back into OpenWebUI's
    # chat-completions API with the model the user selected, so briefs are answered by
    # the same model the user is talking to. That reuses HIVE_OWUI_* above.
    llm_enabled: bool = False
    llm_fallback_model: str | None = Field(
        default=None,
        description="Model id used only when the selected one cannot be determined. "
        "Leave empty to fail with an explicit message instead of quietly using a "
        "different model than the user picked.",
    )
    llm_timeout_seconds: float = 120.0
    llm_max_repair_attempts: int = Field(
        default=1,
        ge=0,
        le=3,
        description="Retries after a schema validation failure, each fed the validation "
        "error. One is usually enough; more mostly burns tokens.",
    )
    llm_max_output_tokens: int = 8000

    # --- Derived ---------------------------------------------------------------
    @property
    def templates_dir(self) -> Path:
        return self.data_dir / "templates"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def audit_dir(self) -> Path:
        return self.data_dir / "audit"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def auth_enabled(self) -> bool:
        # Truthiness, not `is not None`: belt and braces with the validator above, since
        # getting this wrong locks every caller out with a 401 that looks like a
        # networking problem.
        return bool(self.auth_token)

    @property
    def owui_configured(self) -> bool:
        return bool(self.owui_url and self.owui_api_key)

    @model_validator(mode="after")
    def _check_prod_invariants(self) -> Settings:
        if self.environment != "prod":
            return self
        missing = [
            name
            for name, value in (
                ("HIVE_AUTH_TOKEN", self.auth_token),
                ("HIVE_OWUI_URL", self.owui_url),
                ("HIVE_OWUI_API_KEY", self.owui_api_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"HIVE_ENVIRONMENT=prod requires: {', '.join(missing)}. "
                "Refusing to start unauthenticated."
            )
        if self.llm_enabled and not self.owui_configured:
            raise ValueError(
                "HIVE_LLM_ENABLED=true requires HIVE_OWUI_URL and HIVE_OWUI_API_KEY: "
                "brief expansion runs through OpenWebUI's own chat-completions API."
            )
        return self

    def ensure_dirs(self) -> None:
        for path in (self.templates_dir, self.tmp_dir, self.audit_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
