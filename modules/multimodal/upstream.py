from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import AsyncGenerator, AsyncIterator

import anyio
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import AuthStyle, Settings
from .lifecycle import close_resource, set_request_stage, track_resource


logger = logging.getLogger("mm_bridge")

CONNECTION_RETRY_DELAY_SECONDS = 0.2
EMPTY_RESPONSE_DISCONNECT = "Server disconnected without sending a response."
RETRYABLE_GENERATION_PATHS = {
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/responses",
}


# SSE 이벤트는 빈 줄로 구분된다.
# \n\n, \r\n\r\n, \r\r 형식을 모두 처리한다.
SSE_EVENT_BOUNDARY_RE = re.compile(
    rb"(?:\r\n|\r|\n)(?:\r\n|\r|\n)"
)

# SSE comment 형식이다.
# 정상적인 SSE 파서는 이 내용을 사용자 메시지로 처리하지 않고 무시한다.
SSE_HEARTBEAT = b": mm-bridge-keepalive\n\n"


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


@dataclass
class UpstreamResult:
    status_code: int
    headers: dict[str, str]
    body: bytes
    url: str
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> object | None:
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return None

    def text(self) -> str:
        try:
            return self.body.decode("utf-8", errors="replace")
        except Exception:
            return repr(self.body)


class UpstreamTimeoutError(Exception):
    def __init__(
        self,
        *,
        stage: str,
        request_id: str,
        elapsed_ms: int,
    ) -> None:
        super().__init__(f"upstream timeout during {stage}")
        self.stage = stage
        self.request_id = request_id
        self.elapsed_ms = elapsed_ms
        self.analyzer_source: str | None = None


class UpstreamRequestError(Exception):
    def __init__(
        self,
        *,
        stage: str,
        request_id: str,
        elapsed_ms: int,
        error_type: str,
    ) -> None:
        super().__init__(f"upstream request failed during {stage}: {error_type}")
        self.stage = stage
        self.request_id = request_id
        self.elapsed_ms = elapsed_ms
        self.error_type = error_type
        self.analyzer_source: str | None = None


def build_upstream_url(root_url: str, path: str, query: str = "") -> str:
    root = root_url.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    url = root + path
    if query:
        url += "?" + query
    return url


def prepare_headers(request: Request, api_key: str, auth_style: AuthStyle, path: str, request_id: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in HOP_BY_HOP_HEADERS:
            continue
        if lk in {"authorization", "x-api-key"}:
            continue
        headers[k] = v
    headers["x-mm-bridge-request-id"] = request_id
    add_upstream_auth(headers, api_key, auth_style, path)
    return headers


def add_upstream_auth(headers: dict[str, str], api_key: str, auth_style: AuthStyle, path: str) -> None:
    if auth_style == "none" or not api_key:
        return
    style = auth_style
    if style == "auto":
        style = "x-api-key" if path.rstrip("/") in {"/v1/messages", "/v1/messages/count_tokens"} else "bearer"
    if style in {"bearer", "both"}:
        headers["Authorization"] = f"Bearer {api_key}"
    if style in {"x-api-key", "both"}:
        headers["x-api-key"] = api_key


def response_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in HOP_BY_HOP_HEADERS:
            continue
        out[k] = v
    return out


def _retryable_connection_error(exc: httpx.RequestError) -> bool:
    return isinstance(exc, httpx.ConnectError) or (
        isinstance(exc, httpx.RemoteProtocolError)
        and str(exc) == EMPTY_RESPONSE_DISCONNECT
    )


def _connection_error_causes(exc: BaseException) -> str:
    """Log exception types and OS codes without credentials or response bodies."""
    pending = [exc]
    seen: set[int] = set()
    details: list[str] = []
    while pending and len(seen) < 16:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        detail = type(current).__name__
        if isinstance(current, OSError):
            detail += f"(errno={current.errno},winerror={getattr(current, 'winerror', None)})"
        details.append(detail)
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)
        # ExceptionGroup on Python 3.11+, without requiring it on Python 3.10.
        pending.extend(getattr(current, "exceptions", ()))
    return ">".join(details)


async def _send_with_connection_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    stage: str,
    request_id: str,
) -> httpx.Response:
    """Return after response headers; never retry response-body consumption."""
    set_request_stage(stage)
    target = httpx.URL(url)
    target_port = target.port or (443 if target.scheme == "https" else 80)
    method = method.upper()
    can_retry = method in {"GET", "HEAD"} or (
        method == "POST" and target.path.rstrip("/") in RETRYABLE_GENERATION_PATHS
    )
    started = time.perf_counter()
    for attempt in (1, 2):
        # A new Request replays the buffered body, not a consumed request stream.
        request = client.build_request(method, url, headers=headers, content=body)
        connection = "unknown"
        phase = "send"
        headers_received = False
        transport_causes = ""

        async def trace(name: str, info: dict[str, object]) -> None:
            nonlocal connection, phase, headers_received, transport_causes
            if name in {"connection.connect_tcp.started", "connection.connect_unix_socket.started"}:
                connection = "new"
                phase = "connect"
            elif name == "connection.start_tls.started":
                phase = "tls"
            elif name.endswith(".send_request_headers.started"):
                if connection == "unknown":
                    connection = "reused"
                phase = "request_headers"
            elif name.endswith(".send_request_body.started"):
                phase = "request_body"
            elif name.endswith(".receive_response_headers.started"):
                phase = "response_headers"
            elif name.endswith(".receive_response_headers.complete"):
                headers_received = True
            elif name.endswith(".failed"):
                failure = info.get("exception")
                if isinstance(failure, BaseException):
                    # httpcore's pool can re-raise from None, losing the OS cause.
                    # Capture it at the transport failure before that happens.
                    causes = _connection_error_causes(failure)
                    if not transport_causes or "errno=" in causes:
                        transport_causes = causes

        request.extensions["trace"] = trace
        try:
            # stream=True is also required for non-stream client requests: reading
            # the body must happen outside this retry loop, after headers arrive.
            response = await client.send(request, stream=True)
        except httpx.RequestError as exc:
            retry = (
                attempt == 1
                and can_retry
                and not headers_received
                and _retryable_connection_error(exc)
            )
            logger.warning(
                "request_id=%s upstream_connection_%s stage=%s target_host=%s "
                "target_port=%s method=%s attempt=%s connection=%s phase=%s "
                "elapsed_ms=%s error_type=%s causes=%s retry_delay_ms=%s",
                request_id,
                "retry" if retry else "failed",
                stage,
                target.host,
                target_port,
                method,
                attempt,
                connection,
                phase,
                int((time.perf_counter() - started) * 1000),
                type(exc).__name__,
                transport_causes or _connection_error_causes(exc),
                int(CONNECTION_RETRY_DELAY_SECONDS * 1000) if retry else 0,
            )
            if not retry:
                raise
            # HTTPX discards the failed connection. Do not close the shared
            # client/pool, which may still be serving other concurrent requests.
            await asyncio.sleep(CONNECTION_RETRY_DELAY_SECONDS)
        else:
            track_resource(response)
            if attempt == 2:
                logger.info(
                    "request_id=%s upstream_connection_recovered stage=%s "
                    "target_host=%s target_port=%s attempt=2 connection=%s elapsed_ms=%s",
                    request_id,
                    stage,
                    target.host,
                    target_port,
                    connection,
                    int((time.perf_counter() - started) * 1000),
                )
            return response
    raise AssertionError("connection retry loop exhausted")


async def request_bytes(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    stage: str,
    request_id: str,
) -> UpstreamResult:
    started = time.perf_counter()
    logger.info(
        "request_id=%s upstream_start stage=%s method=%s request_bytes=%s",
        request_id,
        stage,
        method,
        len(body),
    )
    try:
        resp = await _send_with_connection_retry(
            client, method, url, headers, body, stage=stage, request_id=request_id
        )
        try:
            await resp.aread()
        finally:
            await close_resource(resp)
    except httpx.TimeoutException as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.warning(
            "request_id=%s upstream_timeout stage=%s elapsed_ms=%s",
            request_id,
            stage,
            elapsed_ms,
        )
        raise UpstreamTimeoutError(
            stage=stage,
            request_id=request_id,
            elapsed_ms=elapsed_ms,
        ) from exc
    except httpx.RequestError as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.warning(
            "request_id=%s upstream_request_error stage=%s "
            "elapsed_ms=%s error_type=%s",
            request_id,
            stage,
            elapsed_ms,
            type(exc).__name__,
        )
        raise UpstreamRequestError(
            stage=stage,
            request_id=request_id,
            elapsed_ms=elapsed_ms,
            error_type=type(exc).__name__,
        ) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result = UpstreamResult(
        status_code=resp.status_code,
        headers=dict(resp.headers),
        body=resp.content,
        url=url,
        elapsed_ms=elapsed_ms,
    )
    logger.info(
        "request_id=%s upstream_complete stage=%s status=%s "
        "elapsed_ms=%s request_bytes=%s response_bytes=%s",
        request_id,
        stage,
        result.status_code,
        elapsed_ms,
        len(body),
        len(result.body),
    )
    return result


def make_response(result: UpstreamResult, stage: str, request_id: str, settings: Settings) -> Response:
    headers = response_headers(result.headers)
    headers["x-mm-bridge-stage"] = stage
    headers["x-mm-bridge-request-id"] = request_id

    if settings.wrap_upstream_errors and result.status_code >= 400:
        return JSONResponse(
            {
                "error": {
                    "stage": stage,
                    "upstream_url": result.url,
                    "status_code": result.status_code,
                    "body": result.text(),
                }
            },
            status_code=result.status_code,
            headers={"x-mm-bridge-stage": stage, "x-mm-bridge-request-id": request_id},
        )

    content_type = headers.get("content-type") or headers.get("Content-Type")
    return Response(
        content=result.body,
        status_code=result.status_code,
        headers=headers,
        media_type=content_type,
    )

async def iter_sse_with_heartbeat(
    source: AsyncIterator[bytes],
    heartbeat_seconds: float,
) -> AsyncGenerator[bytes, None]:
    """SSE 이벤트 경계를 보존하면서 idle 구간에 heartbeat를 전송한다.

    upstream chunk가 SSE 이벤트 중간에서 잘릴 수 있으므로,
    완성된 이벤트의 빈 줄 경계를 확인한 뒤 downstream으로 보낸다.

    wait_for()를 사용하면 timeout 때 upstream read task가 취소될 수 있으므로,
    하나의 pending task를 유지하면서 asyncio.wait()로 감시한다.
    """

    iterator = source.__aiter__()

    pending: asyncio.Task[bytes] | None = asyncio.create_task(
        anext(iterator)
    )

    buffer = bytearray()

    try:
        while pending is not None:
            timeout = (
                heartbeat_seconds
                if heartbeat_seconds > 0
                else None
            )

            done, _ = await asyncio.wait(
                {pending},
                timeout=timeout,
            )

            # 아직 upstream 데이터가 도착하지 않았다.
            # pending read는 취소하지 않고 heartbeat만 보낸다.
            if not done:
                yield SSE_HEARTBEAT
                continue

            try:
                chunk = pending.result()
            except StopAsyncIteration:
                pending = None
                break

            # 다음 upstream chunk 읽기를 미리 시작한다.
            pending = asyncio.create_task(
                anext(iterator)
            )

            if not chunk:
                continue

            buffer.extend(chunk)

            # 완성된 SSE 이벤트만 downstream으로 전달한다.
            while True:
                match = SSE_EVENT_BOUNDARY_RE.search(buffer)

                if match is None:
                    break

                event_end = match.end()

                yield bytes(buffer[:event_end])

                del buffer[:event_end]

        # upstream이 정상적으로 종료됐는데 마지막 데이터에
        # 빈 줄이 없다면 남은 데이터도 손실 없이 전달한다.
        if buffer:
            yield bytes(buffer)

    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            with anyio.CancelScope(shield=True):
                with suppress(asyncio.CancelledError, Exception):
                    await pending

async def stream_upstream(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    stage: str,
    request_id: str,
    heartbeat_seconds: float = 15.0,
) -> StreamingResponse:
    started = time.perf_counter()

    # 원본 headers 객체를 직접 변경하지 않도록 복사한다.
    upstream_headers = dict(headers)

    # 압축된 SSE 스트림 중간에 평문 heartbeat를 삽입하면
    # 압축 데이터가 깨질 수 있으므로 upstream 압축을 요청하지 않는다.
    upstream_headers["accept-encoding"] = "identity"

    logger.info(
        "request_id=%s stream_connect_start stage=%s",
        request_id,
        stage,
    )

    resp = await _send_with_connection_retry(
        client,
        method,
        url,
        upstream_headers,
        body,
        stage=stage,
        request_id=request_id,
    )

    content_type = resp.headers.get(
        "content-type",
        "",
    )

    content_encoding = resp.headers.get(
        "content-encoding",
        "",
    ).strip().lower()

    is_sse = (
        "text/event-stream"
        in content_type.lower()
    )

    # accept-encoding: identity를 보냈는데도 upstream이 압축했다면,
    # httpx에서 압축을 해제한 bytes를 사용한다.
    decode_upstream = (
        bool(content_encoding)
        and content_encoding != "identity"
    )

    logger.info(
        "request_id=%s stream_headers "
        "stage=%s status=%s elapsed_ms=%s "
        "content_type=%s content_encoding=%s",
        request_id,
        stage,
        resp.status_code,
        int(
            (time.perf_counter() - started) * 1000
        ),
        content_type or "unknown",
        content_encoding or "identity",
    )

    async def iterator() -> AsyncGenerator[bytes, None]:
        heartbeat_count = 0
        first_payload_seen = False

        # 압축 응답이면 aiter_bytes()가 압축을 해제한다.
        # identity 응답이면 원본 bytes를 그대로 사용한다.
        source: AsyncIterator[bytes]

        if decode_upstream:
            source = resp.aiter_bytes()
        else:
            source = resp.aiter_raw()

        stream: AsyncIterator[bytes]

        if is_sse and heartbeat_seconds > 0:
            stream = iter_sse_with_heartbeat(
                source,
                heartbeat_seconds,
            )
        else:
            stream = source

        try:
            async for chunk in stream:
                if chunk == SSE_HEARTBEAT:
                    heartbeat_count += 1

                    # 로그가 너무 많아지는 것을 막기 위해
                    # 첫 번째와 10회 단위만 기록한다.
                    if (
                        heartbeat_count == 1
                        or heartbeat_count % 10 == 0
                    ):
                        logger.info(
                            "request_id=%s "
                            "stream_heartbeat "
                            "stage=%s count=%s",
                            request_id,
                            stage,
                            heartbeat_count,
                        )

                elif not first_payload_seen:
                    first_payload_seen = True

                    logger.info(
                        "request_id=%s "
                        "stream_first_payload "
                        "stage=%s elapsed_ms=%s "
                        "bytes=%s",
                        request_id,
                        stage,
                        int(
                            (
                                time.perf_counter()
                                - started
                            )
                            * 1000
                        ),
                        len(chunk),
                    )

                yield chunk

        except asyncio.CancelledError:
            logger.warning(
                "request_id=%s stream_cancelled "
                "stage=%s "
                "likely_client_disconnect=1",
                request_id,
                stage,
            )
            raise

        except Exception:
            logger.exception(
                "request_id=%s stream_error "
                "stage=%s",
                request_id,
                stage,
            )
            raise

        finally:
            with anyio.CancelScope(shield=True):
                try:
                    # Closing an outer async generator does not automatically
                    # close the inner heartbeat generator and its pending read.
                    close_stream = getattr(stream, "aclose", None)
                    if close_stream is not None:
                        await close_stream()
                finally:
                    await close_resource(resp)

            logger.info(
                "request_id=%s stream_closed "
                "stage=%s total_ms=%s "
                "heartbeats=%s",
                request_id,
                stage,
                int(
                    (
                        time.perf_counter()
                        - started
                    )
                    * 1000
                ),
                heartbeat_count,
            )

    out_headers = response_headers(
        dict(resp.headers)
    )

    # aiter_bytes()로 압축을 해제했다면 원본 Content-Encoding을
    # downstream으로 보내면 안 된다.
    if decode_upstream:
        out_headers.pop(
            "content-encoding",
            None,
        )

    out_headers["x-mm-bridge-stage"] = stage
    out_headers["x-mm-bridge-request-id"] = request_id

    if is_sse:
        # 프록시가 SSE 데이터를 변형하거나 버퍼링하는 것을 줄인다.
        out_headers["cache-control"] = (
            "no-cache, no-transform"
        )
        out_headers["x-accel-buffering"] = "no"

    # Content-Type은 upstream response header에 이미 있으므로
    # media_type을 다시 지정하지 않는다.
    body_iterator = iterator()
    track_resource(body_iterator)
    return StreamingResponse(
        body_iterator,
        status_code=resp.status_code,
        headers=out_headers,
    )
