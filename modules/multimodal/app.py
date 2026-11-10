from __future__ import annotations

import copy
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .analysis_contract import AnalysisResult, process_analysis_text
from .cache import AnalysisCache, make_cache_key
from .config import Settings
from .context import extract_current_request_context
from .lifecycle import ClientDisconnectMiddleware
from .media import MediaItem, replace_or_strip_media_blocks
from .prompting import (
    analyzer_truncation_reason,
    build_analyzer_body,
    extract_text_from_upstream_response,
    inject_analysis,
    rewrite_model,
)
from .security import assert_body_size, check_client_auth
from .upstream import (
    UpstreamResult,
    UpstreamRequestError,
    UpstreamTimeoutError,
    build_upstream_url,
    make_response,
    prepare_headers,
    request_bytes,
    stream_upstream,
)

logger = logging.getLogger("mm_bridge")
SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def create_app(settings: Settings) -> FastAPI:
    if settings.bridge_mode not in {1, 2}:
        raise ValueError("MM_BRIDGE_MODE must be 1 or 2")
    app = FastAPI(title="MM Bridge Official Transparent Proxy", version="2.0.0")
    app.add_middleware(ClientDisconnectMiddleware)
    timeout = httpx.Timeout(settings.http_timeout_seconds, connect=min(30.0, settings.http_timeout_seconds))
    client = httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=settings.http_keepalive_expiry_seconds,
        ),
        follow_redirects=False,
    )
    cache = None if settings.bridge_mode == 2 else AnalysisCache(settings.analyzer_cache_dir)

    app.state.settings = settings
    app.state.http_client = client
    app.state.cache = cache

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await client.aclose()

    @app.exception_handler(UpstreamTimeoutError)
    async def _upstream_timeout_handler(
        request: Request,
        exc: UpstreamTimeoutError,
    ) -> JSONResponse:
        headers = {
            "x-mm-bridge-stage": exc.stage,
            "x-mm-bridge-request-id": exc.request_id,
        }
        if exc.analyzer_source:
            headers["x-mm-bridge-analyzer-source"] = exc.analyzer_source
        return JSONResponse(
            {
                "error": {
                    "type": "upstream_timeout",
                    "stage": exc.stage,
                    "request_id": exc.request_id,
                    "elapsed_ms": exc.elapsed_ms,
                }
            },
            status_code=504,
            headers=headers,
        )

    @app.exception_handler(UpstreamRequestError)
    async def _upstream_request_error_handler(
        request: Request,
        exc: UpstreamRequestError,
    ) -> JSONResponse:
        headers = {
            "x-mm-bridge-stage": exc.stage,
            "x-mm-bridge-request-id": exc.request_id,
        }
        if exc.analyzer_source:
            headers["x-mm-bridge-analyzer-source"] = exc.analyzer_source
        return JSONResponse(
            {
                "error": {
                    "type": "upstream_request_error",
                    "stage": exc.stage,
                    "request_id": exc.request_id,
                    "elapsed_ms": exc.elapsed_ms,
                    "transport_error": exc.error_type,
                }
            },
            status_code=502,
            headers=headers,
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "mode": settings.bridge_mode,
            "analyzer_enabled": settings.bridge_mode == 1,
            "port": settings.bridge_port,
            "bridge_model_id": settings.bridge_model_id,
            "vllm_media_policy": settings.vllm_media_policy,
            "stream_heartbeat_seconds": settings.stream_heartbeat_seconds,
        }

    @app.api_route("/v1/models", methods=["GET"])
    async def models(request: Request) -> Response:
        check_client_auth(request, settings)
        request_id = _request_id(request)
        assert_body_size(await request.body(), settings)
        if settings.models_policy == "passthrough":
            return await _forward_raw(request, settings, client, request_id, stage="vllm_models")
        return await _models_response(request, settings, client, request_id)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def transparent(path: str, request: Request) -> Response:
        check_client_auth(request, settings)
        request_id = _request_id(request)
        started = time.perf_counter()
        endpoint = "/" + path
        body_bytes = await request.body()
        assert_body_size(body_bytes, settings)

        if request.method.upper() != "POST" or not _looks_json(request, body_bytes):
            logger.info("request_id=%s direct method=%s path=%s bytes=%s", request_id, request.method, endpoint, len(body_bytes))
            return await _forward_raw(request, settings, client, request_id, stage="vllm_direct", body_bytes=body_bytes)

        try:
            body = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            logger.info("request_id=%s direct non_json path=%s bytes=%s", request_id, endpoint, len(body_bytes))
            return await _forward_raw(request, settings, client, request_id, stage="vllm_direct", body_bytes=body_bytes)

        if not isinstance(body, dict):
            return await _forward_raw(request, settings, client, request_id, stage="vllm_direct", body_bytes=body_bytes)

        if settings.bridge_mode == 2:
            final_body = rewrite_model(
                body,
                await _vllm_model(settings, client, request_id),
                settings.bridge_model_id,
                settings.rewrite_bridge_model_only,
            )
            return await _forward_json_to_vllm(
                request,
                settings,
                client,
                request_id,
                _json_bytes(final_body),
                stage="vllm_direct",
            )

        assert cache is not None
        request_context = extract_current_request_context(body, endpoint)
        media_items = request_context.media_items
        if len(media_items) > settings.max_media_items:
            raise HTTPException(
                status_code=413,
                detail={
                    "error": "too_many_media_items",
                    "media_items": len(media_items),
                    "max_media_items": settings.max_media_items,
                    "request_id": request_id,
                },
            )
        for item in media_items:
            if item.approx_bytes > settings.max_media_bytes:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "error": "media_item_too_large",
                        "media_index": item.index,
                        "approx_bytes": item.approx_bytes,
                        "max_media_bytes": settings.max_media_bytes,
                        "request_id": request_id,
                    },
                )

        if not media_items:
            final_body = replace_or_strip_media_blocks(body, "strip")
            final_body = rewrite_model(final_body, await _vllm_model(settings, client, request_id), settings.bridge_model_id, settings.rewrite_bridge_model_only)
            final_bytes = _json_bytes(final_body)
            _debug_dump(settings, request_id, "incoming_direct.json", body)
            logger.info("request_id=%s vllm_direct path=%s media=0 bytes=%s", request_id, endpoint, len(final_bytes))
            return await _forward_json_to_vllm(request, settings, client, request_id, final_bytes, stage="vllm_direct")

        normalized_endpoint = endpoint.rstrip("/")
        if normalized_endpoint not in {p.rstrip("/") for p in settings.analyzer_endpoints}:
            msg = f"media detected but endpoint {endpoint} is not in MM_ANALYZER_ENDPOINTS; official analyzer enrichment skipped"
            logger.warning("request_id=%s %s", request_id, msg)
            if settings.unsupported_media_endpoint_policy == "error":
                return JSONResponse(
                    {
                        "error": {
                            "stage": "bridge_protocol_gate",
                            "message": msg,
                            "request_id": request_id,
                            "supported_analyzer_endpoints": settings.analyzer_endpoints,
                        }
                    },
                    status_code=501,
                )
            final_body = rewrite_model(body, await _vllm_model(settings, client, request_id), settings.bridge_model_id, settings.rewrite_bridge_model_only)
            return await _forward_json_to_vllm(request, settings, client, request_id, _json_bytes(final_body), stage="vllm_passthrough_media_unsupported_endpoint")

        logger.info(
            "request_id=%s media_detected path=%s origin=%s media_count=%s policy=%s",
            request_id,
            endpoint,
            request_context.event_kind,
            len(media_items),
            settings.vllm_media_policy,
        )
        _debug_dump(settings, request_id, "incoming.json", body)
        _debug_dump(
            settings,
            request_id,
            "incoming_media_meta.json",
            _media_debug_metadata(media_items),
        )

        try:
            llama_model = await _llama_model(settings, client, request_id)
        except (UpstreamTimeoutError, UpstreamRequestError) as exc:
            if settings.fail_on_analyzer_error:
                raise
            logger.error(
                "request_id=%s analyzer_discovery_fail_open "
                "stage=%s elapsed_ms=%s",
                request_id,
                exc.stage,
                exc.elapsed_ms,
            )
            return await _forward_analyzer_fail_open(
                request,
                settings,
                client,
                request_id,
                body,
                reason=exc.stage,
            )
        user_request_text = request_context.user_request_text
        cache_key = make_cache_key(normalized_endpoint, llama_model, media_items, user_request_text)
        analysis_text = cache.get(cache_key) if settings.analyzer_cache else None
        analysis_source: str | None = None
        fail_open = False
        if analysis_text:
            try:
                analysis_result = process_analysis_text(analysis_text, media_items)
                analysis_text = analysis_result.analysis_text
                _record_analysis_result(
                    settings,
                    request_id,
                    analysis_result,
                    source="cache",
                )
                analysis_source = "cache"
                logger.info("request_id=%s analyzer_cache_hit key=%s", request_id, cache_key[:16])
            except ValueError as exc:
                logger.warning(
                    "request_id=%s analyzer_cache_invalid key=%s error=%s",
                    request_id,
                    cache_key[:16],
                    exc,
                )
                analysis_text = None
        if not analysis_text:
            analyzer_body = build_analyzer_body(
                body,
                normalized_endpoint,
                media_items,
                settings,
                llama_model,
                user_request_text=user_request_text,
            )
            _debug_dump(settings, request_id, "llama_request.json", analyzer_body)
            _debug_dump(
                settings,
                request_id,
                "analyzer_request_meta.json",
                _analyzer_request_debug_metadata(
                    normalized_endpoint,
                    analyzer_body,
                    media_items,
                ),
            )
            try:
                analyzer_result = await _call_llama_analyzer(
                    request,
                    settings,
                    client,
                    request_id,
                    endpoint,
                    analyzer_body,
                )
            except (UpstreamTimeoutError, UpstreamRequestError) as exc:
                if settings.fail_on_analyzer_error:
                    raise
                logger.error(
                    "request_id=%s analyzer_request_fail_open "
                    "error_type=%s elapsed_ms=%s",
                    request_id,
                    type(exc).__name__,
                    exc.elapsed_ms,
                )
                status_code = (
                    504 if isinstance(exc, UpstreamTimeoutError) else 502
                )
                analyzer_result = UpstreamResult(
                    status_code=status_code,
                    headers={},
                    body=_json_bytes(
                        {
                            "error": "analyzer_request_failed",
                            "error_type": type(exc).__name__,
                            "elapsed_ms": exc.elapsed_ms,
                        }
                    ),
                    url=build_upstream_url(
                        settings.llama_root_url,
                        endpoint,
                        request.url.query,
                    ),
                    elapsed_ms=exc.elapsed_ms,
                )
            _debug_dump_bytes(settings, request_id, "llama_response.body", analyzer_result.body)
            if not analyzer_result.ok:
                logger.error(
                    "request_id=%s llama_analyzer_failed path=%s status=%s",
                    request_id,
                    endpoint,
                    analyzer_result.status_code,
                )
                if settings.fail_on_analyzer_error:
                    error_detail: dict[str, Any] = {
                        "stage": "llama_analyzer",
                        "method": request.method,
                        "path": endpoint,
                        "status_code": analyzer_result.status_code,
                        "request_id": request_id,
                    }
                    if settings.wrap_upstream_errors:
                        error_detail["upstream_url"] = analyzer_result.url
                        error_detail["body"] = analyzer_result.text()
                    return JSONResponse(
                        {"error": error_detail},
                        status_code=analyzer_result.status_code,
                        headers={"x-mm-bridge-stage": "llama_analyzer", "x-mm-bridge-request-id": request_id},
                    )
                # Optional fail-open: final vLLM receives original request.
                analysis_text = ""
                analysis_source = "fail_open"
                fail_open = True
            else:
                try:
                    analysis_payload = analyzer_result.json()
                    truncation_reason = analyzer_truncation_reason(
                        normalized_endpoint, analysis_payload
                    )
                    if truncation_reason:
                        raise ValueError(
                            f"analyzer output was truncated: {truncation_reason}"
                        )
                    extracted = extract_text_from_upstream_response(
                        normalized_endpoint, analysis_payload
                    )
                    analysis_result = process_analysis_text(extracted, media_items)
                    analysis_text = analysis_result.analysis_text
                    _record_analysis_result(
                        settings,
                        request_id,
                        analysis_result,
                        source="analyzer",
                    )
                    analysis_source = "analyzer"
                except (TypeError, ValueError) as exc:
                    _debug_dump(
                        settings,
                        request_id,
                        "validation_result.json",
                        {"ok": False, "source": "analyzer", "error": str(exc)},
                    )
                    logger.error(
                        "request_id=%s analyzer_output_invalid path=%s error=%s",
                        request_id,
                        endpoint,
                        exc,
                    )
                    if settings.fail_on_analyzer_error:
                        return JSONResponse(
                            {
                                "error": {
                                    "stage": "llama_analyzer_validation",
                                    "path": endpoint,
                                    "message": str(exc),
                                    "request_id": request_id,
                                }
                            },
                            status_code=502,
                            headers={
                                "x-mm-bridge-stage": "llama_analyzer_validation",
                                "x-mm-bridge-request-id": request_id,
                            },
                        )
                    analysis_text = ""
                    analysis_source = "fail_open"
                    fail_open = True
                if analysis_text and settings.analyzer_cache:
                    cache.set(
                        cache_key,
                        analysis_text,
                        {
                            "endpoint": normalized_endpoint,
                            "model": llama_model,
                            "event_kind": request_context.event_kind,
                        },
                    )

        if fail_open:
            final_body = copy.deepcopy(body)
        else:
            final_body = inject_analysis(
                body,
                normalized_endpoint,
                analysis_text or "",
                settings,
            )
        final_body = rewrite_model(final_body, await _vllm_model(settings, client, request_id), settings.bridge_model_id, settings.rewrite_bridge_model_only)
        final_bytes = _json_bytes(final_body)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "request_id=%s analyzer_route source=%s cache_key=%s",
            request_id,
            analysis_source,
            cache_key[:16],
        )
        logger.info("request_id=%s vllm_final path=%s bytes=%s elapsed_before_vllm_ms=%s", request_id, endpoint, len(final_bytes), elapsed_ms)
        return await _forward_json_to_vllm(
            request,
            settings,
            client,
            request_id,
            final_bytes,
            stage="vllm_final",
            analyzer_source=analysis_source,
        )

    return app


async def _models_response(request: Request, settings: Settings, client: httpx.AsyncClient, request_id: str) -> Response:
    upstream_data: list[dict[str, Any]] = []
    upstream_status = 200
    if settings.models_policy in {"alias_plus_upstream", "upstream_only"}:
        url = build_upstream_url(settings.vllm_root_url, "/v1/models", request.url.query)
        headers = prepare_headers(request, settings.vllm_api_key, settings.vllm_auth_style, "/v1/models", request_id)
        try:
            result = await request_bytes(
                client,
                "GET",
                url,
                headers,
                b"",
                stage="vllm_models",
                request_id=request_id,
            )
            upstream_status = result.status_code
            if result.ok:
                payload = result.json()
                if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                    upstream_data = [x for x in payload["data"] if isinstance(x, dict)]
            elif settings.models_policy == "upstream_only":
                return make_response(result, "vllm_models", request_id, settings)
        except Exception as exc:
            if settings.models_policy == "upstream_only":
                raise
            logger.warning("request_id=%s model_discovery_failed error=%s", request_id, exc)

    if settings.models_policy == "upstream_only":
        return JSONResponse({"object": "list", "data": upstream_data}, headers={"x-mm-bridge-request-id": request_id})

    alias = {
        "id": settings.bridge_model_id,
        "object": "model",
        "created": 0,
        "owned_by": "mm-bridge",
    }
    if settings.models_policy == "alias_only":
        data = [alias]
    else:
        existing = {item.get("id") for item in upstream_data}
        data = ([alias] if settings.bridge_model_id not in existing else []) + upstream_data
    return JSONResponse({"object": "list", "data": data}, headers={"x-mm-bridge-request-id": request_id, "x-mm-bridge-stage": "bridge_models"})


async def _forward_raw(
    request: Request,
    settings: Settings,
    client: httpx.AsyncClient,
    request_id: str,
    stage: str,
    body_bytes: bytes | None = None,
) -> Response:
    if body_bytes is None:
        body_bytes = await request.body()
    endpoint = request.url.path
    url = build_upstream_url(settings.vllm_root_url, endpoint, request.url.query)
    headers = prepare_headers(request, settings.vllm_api_key, settings.vllm_auth_style, endpoint, request_id)
    result = await request_bytes(
        client,
        request.method,
        url,
        headers,
        body_bytes,
        stage=stage,
        request_id=request_id,
    )
    return make_response(result, stage, request_id, settings)


async def _forward_analyzer_fail_open(
    request: Request,
    settings: Settings,
    client: httpx.AsyncClient,
    request_id: str,
    original_body: dict[str, Any],
    *,
    reason: str,
) -> Response:
    final_body = rewrite_model(
        copy.deepcopy(original_body),
        await _vllm_model(settings, client, request_id),
        settings.bridge_model_id,
        settings.rewrite_bridge_model_only,
    )
    final_bytes = _json_bytes(final_body)
    logger.info(
        "request_id=%s analyzer_route source=fail_open reason=%s",
        request_id,
        reason,
    )
    _debug_dump(
        settings,
        request_id,
        "validation_result.json",
        {"ok": False, "source": "fail_open", "reason": reason},
    )
    return await _forward_json_to_vllm(
        request,
        settings,
        client,
        request_id,
        final_bytes,
        stage="vllm_final",
        analyzer_source="fail_open",
    )


async def _forward_json_to_vllm(
    request: Request,
    settings: Settings,
    client: httpx.AsyncClient,
    request_id: str,
    body_bytes: bytes,
    stage: str,
    analyzer_source: str | None = None,
) -> Response:
    endpoint = request.url.path
    url = build_upstream_url(settings.vllm_root_url, endpoint, request.url.query)
    headers = prepare_headers(request, settings.vllm_api_key, settings.vllm_auth_style, endpoint, request_id)
    headers["content-type"] = "application/json"
    # Preserve official streaming behavior for the final upstream only.
    _debug_dump(
        settings,
        request_id,
        "final_request_meta.json",
        _final_request_debug_metadata(
            endpoint,
            body_bytes,
            analyzer_source,
        ),
    )
    if _body_requests_stream(body_bytes):
        stream_started = time.perf_counter()
        try:
            response = await stream_upstream(client, request.method, url, headers, body_bytes, stage, request_id, heartbeat_seconds=settings.stream_heartbeat_seconds)
        except httpx.TimeoutException as exc:
            timeout_error = UpstreamTimeoutError(
                stage=stage,
                request_id=request_id,
                elapsed_ms=int((time.perf_counter() - stream_started) * 1000),
            )
            timeout_error.analyzer_source = analyzer_source
            raise timeout_error from exc
        except httpx.RequestError as exc:
            request_error = UpstreamRequestError(
                stage=stage,
                request_id=request_id,
                elapsed_ms=int((time.perf_counter() - stream_started) * 1000),
                error_type=type(exc).__name__,
            )
            request_error.analyzer_source = analyzer_source
            raise request_error from exc
        if analyzer_source:
            response.headers["x-mm-bridge-analyzer-source"] = analyzer_source
        return response
    try:
        result = await request_bytes(
            client,
            request.method,
            url,
            headers,
            body_bytes,
            stage=stage,
            request_id=request_id,
        )
    except (UpstreamTimeoutError, UpstreamRequestError) as exc:
        exc.analyzer_source = analyzer_source
        raise
    _debug_dump(
        settings,
        request_id,
        "final_response_meta.json",
        _final_response_debug_metadata(result),
    )
    response = make_response(result, stage, request_id, settings)
    if analyzer_source:
        response.headers["x-mm-bridge-analyzer-source"] = analyzer_source
    return response


async def _call_llama_analyzer(
    request: Request,
    settings: Settings,
    client: httpx.AsyncClient,
    request_id: str,
    endpoint: str,
    analyzer_body: dict[str, Any],
) -> UpstreamResult:
    body_bytes = _json_bytes(analyzer_body)
    url = build_upstream_url(settings.llama_root_url, endpoint, request.url.query)
    headers = prepare_headers(request, settings.llama_api_key, settings.llama_auth_style, endpoint, request_id)
    headers["content-type"] = "application/json"
    return await request_bytes(
        client,
        request.method,
        url,
        headers,
        body_bytes,
        stage="llama_analyzer",
        request_id=request_id,
    )


async def _discover_first_model(
    root_url: str,
    api_key: str,
    auth_style: str,
    client: httpx.AsyncClient,
    *,
    stage: str,
    request_id: str,
    endpoint: str = "/v1/models",
) -> str:
    headers: dict[str, str] = {}
    from .upstream import add_upstream_auth

    add_upstream_auth(headers, api_key, auth_style, endpoint)  # type: ignore[arg-type]
    url = build_upstream_url(root_url, endpoint, "")
    result = await request_bytes(
        client,
        "GET",
        url,
        headers,
        b"",
        stage=stage,
        request_id=request_id,
    )
    if not result.ok:
        raise UpstreamRequestError(
            stage=stage,
            request_id=request_id,
            elapsed_ms=result.elapsed_ms,
            error_type=f"model_discovery_http_{result.status_code}",
        )
    payload = result.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("id"):
        return str(data[0]["id"])
    raise UpstreamRequestError(
        stage=stage,
        request_id=request_id,
        elapsed_ms=result.elapsed_ms,
        error_type="model_discovery_empty",
    )


async def _vllm_model(
    settings: Settings,
    client: httpx.AsyncClient,
    request_id: str,
) -> str:
    if settings.vllm_model:
        return settings.vllm_model
    if not hasattr(client, "_mm_bridge_vllm_model"):
        setattr(client, "_mm_bridge_vllm_model", await _discover_first_model(
            settings.vllm_root_url,
            settings.vllm_api_key,
            settings.vllm_auth_style,
            client,
            stage="vllm_model_discovery",
            request_id=request_id,
        ))
    return getattr(client, "_mm_bridge_vllm_model")


async def _llama_model(
    settings: Settings,
    client: httpx.AsyncClient,
    request_id: str,
) -> str:
    if settings.llama_model:
        return settings.llama_model
    if not hasattr(client, "_mm_bridge_llama_model"):
        setattr(client, "_mm_bridge_llama_model", await _discover_first_model(
            settings.llama_root_url,
            settings.llama_api_key,
            settings.llama_auth_style,
            client,
            stage="llama_model_discovery",
            request_id=request_id,
        ))
    return getattr(client, "_mm_bridge_llama_model")


def _request_id(request: Request) -> str:
    cached = getattr(request.state, "mm_bridge_request_id", None)
    if isinstance(cached, str) and SAFE_REQUEST_ID_RE.fullmatch(cached):
        return cached
    supplied = (
        request.headers.get("x-request-id")
        or request.headers.get("x-mm-bridge-request-id")
    )
    if supplied and SAFE_REQUEST_ID_RE.fullmatch(supplied):
        request_id = supplied
    else:
        if supplied:
            logger.warning("rejected unsafe client request id")
        request_id = uuid.uuid4().hex[:16]
    request.state.mm_bridge_request_id = request_id
    return request_id


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _looks_json(request: Request, body: bytes) -> bool:
    content_type = request.headers.get("content-type", "").lower()
    if "json" in content_type:
        return True
    stripped = body.lstrip()
    return stripped.startswith(b"{") or stripped.startswith(b"[")


def _body_requests_stream(body: bytes) -> bool:
    try:
        payload = json.loads(body.decode("utf-8"))
        return isinstance(payload, dict) and payload.get("stream") is True
    except Exception:
        return False


def _debug_dump(settings: Settings, request_id: str, name: str, payload: Any) -> None:
    if not settings.debug_dump:
        return
    if not SAFE_REQUEST_ID_RE.fullmatch(request_id):
        logger.error("refused debug dump for unsafe request id")
        return
    root = Path(settings.debug_dump_dir) / request_id
    root.mkdir(parents=True, exist_ok=True)
    with (root / name).open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _debug_dump_bytes(settings: Settings, request_id: str, name: str, payload: bytes) -> None:
    if not settings.debug_dump:
        return
    if not SAFE_REQUEST_ID_RE.fullmatch(request_id):
        logger.error("refused debug dump for unsafe request id")
        return
    root = Path(settings.debug_dump_dir) / request_id
    root.mkdir(parents=True, exist_ok=True)
    with (root / name).open("wb") as f:
        f.write(payload)


def _record_analysis_result(
    settings: Settings,
    request_id: str,
    result: AnalysisResult,
    *,
    source: str,
) -> None:
    if result.repairs:
        logger.info(
            "request_id=%s analyzer_output_repaired source=%s repairs=%s actions=%s",
            request_id,
            source,
            len(result.repairs),
            ",".join(str(repair.get("action", "unknown")) for repair in result.repairs),
        )
    _debug_dump(
        settings,
        request_id,
        "analyzer_response_parsed.json",
        result.parsed_payload,
    )
    _debug_dump(
        settings,
        request_id,
        "analyzer_response_normalized.json",
        result.payload,
    )
    _debug_dump(
        settings,
        request_id,
        "normalization_repairs.json",
        result.repairs,
    )
    _debug_dump(
        settings,
        request_id,
        "validation_result.json",
        {
            "ok": True,
            "source": source,
            "repair_count": len(result.repairs),
        },
    )


def _media_debug_metadata(media_items: list[MediaItem]) -> list[dict[str, Any]]:
    return [
        {
            "index": item.index,
            "type": item.kind,
            "path": item.path,
            "approx_bytes": item.approx_bytes,
            "sha256_prefix": item.hash[:16],
        }
        for item in media_items
    ]


def _analyzer_request_debug_metadata(
    endpoint: str,
    analyzer_body: dict[str, Any],
    media_items: list[MediaItem],
) -> dict[str, Any]:
    token_field = "max_output_tokens" if endpoint == "/v1/responses" else "max_tokens"
    return {
        "endpoint": endpoint,
        "model": analyzer_body.get("model"),
        "stream": analyzer_body.get("stream"),
        token_field: analyzer_body.get(token_field),
        "media": _media_debug_metadata(media_items),
    }


def _final_request_debug_metadata(
    endpoint: str,
    body_bytes: bytes,
    analyzer_source: str | None,
) -> dict[str, Any]:
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    token_field = "max_output_tokens" if endpoint == "/v1/responses" else "max_tokens"
    return {
        "endpoint": endpoint,
        "model": payload.get("model"),
        "stream": payload.get("stream") is True,
        token_field: payload.get(token_field),
        "max_completion_tokens": payload.get("max_completion_tokens"),
        "request_bytes": len(body_bytes),
        "analyzer_source": analyzer_source,
    }


def _final_response_debug_metadata(result: UpstreamResult) -> dict[str, Any]:
    payload = result.json()
    finish_reason: Any = None
    usage: Any = None
    content_is_null: bool | None = None
    reasoning_content_present = False
    if isinstance(payload, dict):
        usage = payload.get("usage")
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            finish_reason = choice.get("finish_reason")
            message = choice.get("message")
            if isinstance(message, dict):
                content_is_null = message.get("content") is None
                reasoning_content_present = bool(message.get("reasoning_content"))
        elif "stop_reason" in payload:
            finish_reason = payload.get("stop_reason")
            content_is_null = payload.get("content") is None
        elif "status" in payload:
            finish_reason = payload.get("status")
    return {
        "status_code": result.status_code,
        "response_bytes": len(result.body),
        "elapsed_ms": result.elapsed_ms,
        "finish_reason": finish_reason,
        "usage": usage,
        "content_is_null": content_is_null,
        "reasoning_content_present": reasoning_content_present,
    }
