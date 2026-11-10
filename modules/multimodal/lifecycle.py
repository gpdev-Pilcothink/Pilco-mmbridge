from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol

import anyio
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send


logger = logging.getLogger("mm_bridge")


class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


@dataclass
class RequestWork:
    stage: str = "request_body"
    resources: dict[int, AsyncCloseable] = field(default_factory=dict)


_request_work: ContextVar[RequestWork | None] = ContextVar("mm_bridge_request_work", default=None)


def set_request_stage(stage: str) -> None:
    work = _request_work.get()
    if work is not None:
        work.stage = stage


def track_resource(resource: AsyncCloseable) -> None:
    work = _request_work.get()
    if work is not None:
        work.resources[id(resource)] = resource


async def close_resource(resource: AsyncCloseable) -> None:
    # Starlette cancels a streaming task inside an AnyIO cancellation scope.
    # Cleanup must still be allowed to await socket closure in that scope.
    with anyio.CancelScope(shield=True):
        await resource.aclose()
    work = _request_work.get()
    if work is not None:
        work.resources.pop(id(resource), None)


def _disconnect_exception(exc: BaseException) -> bool:
    if isinstance(exc, (ClientDisconnect, asyncio.CancelledError)):
        return True
    # Older Starlette versions wrap streaming errors in ExceptionGroup.
    children = getattr(exc, "exceptions", ())
    return bool(children) and all(_disconnect_exception(child) for child in children)


class ClientDisconnectMiddleware:
    """Cancel only the disconnected request, including upstream header waits.

    The route owns body reads. After its last body chunk, a watcher waits for
    http.disconnect. A lock and cached disconnect event allow StreamingResponse
    to also call receive without stealing body chunks or losing the event.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        work = RequestWork()
        token = _request_work.set(work)
        body_complete = asyncio.Event()
        disconnected = asyncio.Event()
        receive_lock = asyncio.Lock()
        received_bytes = 0
        response_started = False
        response_complete = False
        expect_trailers = False
        started = time.perf_counter()

        async def monitored_receive() -> Message:
            nonlocal received_bytes
            async with receive_lock:
                if disconnected.is_set():
                    return {"type": "http.disconnect"}
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected.set()
                    body_complete.set()
                elif message["type"] == "http.request":
                    received_bytes += len(message.get("body", b""))
                    if not message.get("more_body", False):
                        work.stage = "request_processing"
                        body_complete.set()
                return message

        async def monitored_send(message: Message) -> None:
            nonlocal response_started, response_complete, expect_trailers
            try:
                await send(message)
            except OSError as exc:
                # ASGI 2.4+ can report a disconnect through send before receive.
                disconnected.set()
                body_complete.set()
                raise ClientDisconnect() from exc
            if message["type"] == "http.response.start":
                response_started = True
                expect_trailers = message.get("trailers", False)
            elif message["type"] == "http.response.body":
                if not message.get("more_body", False) and not expect_trailers:
                    response_complete = True
            elif message["type"] == "http.response.trailers":
                if not message.get("more_trailers", False):
                    response_complete = True

        async def watch_disconnect() -> None:
            await body_complete.wait()
            while not disconnected.is_set() and not response_complete:
                await monitored_receive()

        app_task = asyncio.create_task(self.app(scope, monitored_receive, monitored_send))
        watcher = asyncio.create_task(watch_disconnect())
        disconnect_waiter = asyncio.create_task(disconnected.wait())
        tasks = (app_task, watcher, disconnect_waiter)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                await watcher  # Preserve unexpected receive failures.
            if disconnected.is_set() and not response_complete:
                if not app_task.done():
                    app_task.cancel()
            try:
                await app_task
            except BaseException as exc:
                if not (disconnected.is_set() and _disconnect_exception(exc)):
                    raise
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    # A response may have been opened just before cancellation,
                    # before its StreamingResponse iterator ever started. Keep
                    # ownership through that handoff and close iterators first.
                    for resource in reversed(list(work.resources.values())):
                        try:
                            await close_resource(resource)
                        except Exception as exc:
                            logger.warning(
                                "request_id=%s request_cleanup_failed error_type=%s",
                                scope.get("state", {}).get("mm_bridge_request_id", "-"),
                                type(exc).__name__,
                            )
                    if disconnected.is_set() and not response_complete:
                        logger.info(
                            "request_id=%s client_disconnected stage=%s "
                            "response_started=%s received_bytes=%s elapsed_ms=%s",
                            scope.get("state", {}).get("mm_bridge_request_id", "-"),
                            work.stage,
                            int(response_started),
                            received_bytes,
                            int((time.perf_counter() - started) * 1000),
                        )
            finally:
                _request_work.reset(token)
