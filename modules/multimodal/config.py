from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Literal


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _csv(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return [part.strip() for part in raw.split(",") if part.strip()]


def _root_url(value: str) -> str:
    value = value.rstrip("/")
    if value.endswith("/v1"):
        value = value[:-3]
    return value.rstrip("/")


AuthStyle = Literal["auto", "bearer", "x-api-key", "both", "none"]
MediaPolicy = Literal["keep", "replace", "strip"]
UnsupportedMediaPolicy = Literal["passthrough", "error"]
ModelsPolicy = Literal["alias_plus_upstream", "alias_only", "upstream_only", "passthrough"]


@dataclass(frozen=True)
class Settings:
    bridge_host: str = "0.0.0.0"
    bridge_port: int = 18000
    bridge_token: str = ""
    bridge_model_id: str = "deepseek-v4-mm-bridge"
    bridge_mode: int = 1
    log_level: str = "INFO"

    vllm_root_url: str = "http://127.0.0.1:8000"
    vllm_api_key: str = "EMPTY"
    vllm_auth_style: AuthStyle = "auto"
    vllm_model: str = ""

    llama_root_url: str = "http://127.0.0.1:8080"
    llama_api_key: str = "EMPTY"
    llama_auth_style: AuthStyle = "auto"
    llama_model: str = ""

    analyzer_endpoints: list[str] = field(default_factory=lambda: [
        "/v1/chat/completions",
        "/v1/messages",
        "/v1/responses",
    ])
    analyzer_force_stream_false: bool = True
    analyzer_response_format_json: bool = True
    analyzer_max_tokens: int = 1200
    analyzer_temperature: float = 0.0
    analyzer_cache: bool = True
    analyzer_cache_dir: str = ".cache/mm_bridge"
    fail_on_analyzer_error: bool = True

    vllm_media_policy: MediaPolicy = "keep"
    unsupported_media_endpoint_policy: UnsupportedMediaPolicy = "passthrough"
    models_policy: ModelsPolicy = "alias_plus_upstream"
    rewrite_bridge_model_only: bool = True

    max_body_bytes: int = 104_857_600
    max_media_bytes: int = 52_428_800
    max_media_items: int = 8
    http_timeout_seconds: float = 600.0
    http_keepalive_expiry_seconds: float = 2.0

    # Cloudflare/Nginx가 장시간 아무 데이터도 없는 SSE 연결을
    # 종료하지 않도록 보내는 heartbeat 주기.
    # 0 이하로 설정하면 heartbeat를 비활성화한다.
    stream_heartbeat_seconds: float = 15.0

    debug_dump: bool = False
    debug_dump_dir: str = ".debug/mm_bridge"
    wrap_upstream_errors: bool = False

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.http_keepalive_expiry_seconds)
            or self.http_keepalive_expiry_seconds < 0
        ):
            raise ValueError("MM_HTTP_KEEPALIVE_EXPIRY_SECONDS must be finite and >= 0")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bridge_host=os.getenv("MM_BRIDGE_HOST", "0.0.0.0"),
            bridge_port=_int("MM_BRIDGE_PORT", 18000),
            bridge_token=os.getenv("MM_BRIDGE_TOKEN", ""),
            bridge_model_id=os.getenv("MM_BRIDGE_MODEL_ID", "deepseek-v4-mm-bridge"),
            bridge_mode=_int("MM_BRIDGE_MODE", 1),
            log_level=os.getenv("MM_LOG_LEVEL", "INFO"),
            vllm_root_url=_root_url(os.getenv("VLLM_ROOT_URL", os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000"))),
            vllm_api_key=os.getenv("VLLM_API_KEY", "EMPTY"),
            vllm_auth_style=os.getenv("VLLM_AUTH_STYLE", "auto"),  # type: ignore[arg-type]
            vllm_model=os.getenv("VLLM_MODEL", ""),
            llama_root_url=_root_url(os.getenv("LLAMA_ROOT_URL", os.getenv("JETSON_LLAMA_BASE_URL", "http://127.0.0.1:8080"))),
            llama_api_key=os.getenv("LLAMA_API_KEY", os.getenv("JETSON_LLAMA_API_KEY", "EMPTY")),
            llama_auth_style=os.getenv("LLAMA_AUTH_STYLE", "auto"),  # type: ignore[arg-type]
            llama_model=os.getenv("LLAMA_MODEL", os.getenv("JETSON_LLAMA_MODEL", "")),
            analyzer_endpoints=_csv("MM_ANALYZER_ENDPOINTS", ["/v1/chat/completions", "/v1/messages", "/v1/responses"]),
            analyzer_force_stream_false=_bool("MM_ANALYZER_FORCE_STREAM_FALSE", True),
            analyzer_response_format_json=_bool("MM_ANALYZER_RESPONSE_FORMAT_JSON", True),
            analyzer_max_tokens=_int("MM_ANALYZER_MAX_TOKENS", 1200),
            analyzer_temperature=_float("MM_ANALYZER_TEMPERATURE", 0.0),
            analyzer_cache=_bool("MM_ANALYZER_CACHE", True),
            analyzer_cache_dir=os.getenv("MM_ANALYZER_CACHE_DIR", os.getenv("MM_MEDIA_CACHE_DIR", ".cache/mm_bridge")),
            fail_on_analyzer_error=_bool("MM_FAIL_ON_ANALYZER_ERROR", True),
            vllm_media_policy=os.getenv("MM_VLLM_MEDIA_POLICY", "keep"),  # type: ignore[arg-type]
            unsupported_media_endpoint_policy=os.getenv("MM_UNSUPPORTED_MEDIA_ENDPOINT_POLICY", "passthrough"),  # type: ignore[arg-type]
            models_policy=os.getenv("MM_MODELS_POLICY", "alias_plus_upstream"),  # type: ignore[arg-type]
            rewrite_bridge_model_only=_bool("MM_REWRITE_BRIDGE_MODEL_ONLY", True),
            max_body_bytes=_int("MM_MAX_BODY_BYTES", 104_857_600),
            max_media_bytes=_int("MM_MAX_MEDIA_BYTES", 52_428_800),
            max_media_items=_int("MM_MAX_MEDIA_ITEMS", 8),
            http_timeout_seconds=_float("MM_HTTP_TIMEOUT_SECONDS", 600.0),
            http_keepalive_expiry_seconds=_float("MM_HTTP_KEEPALIVE_EXPIRY_SECONDS", 2.0),
            stream_heartbeat_seconds=_float("MM_STREAM_HEARTBEAT_SECONDS", 15.0),
            debug_dump=_bool("MM_DEBUG_DUMP", False),
            debug_dump_dir=os.getenv("MM_DEBUG_DUMP_DIR", ".debug/mm_bridge"),
            wrap_upstream_errors=_bool("MM_WRAP_UPSTREAM_ERRORS", False),
        )
