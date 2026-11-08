from __future__ import annotations

import asyncio
import logging
import os
import unittest
from unittest.mock import patch

import httpcore
import httpx

from modules.multimodal import app as bridge_app
from modules.multimodal.config import Settings
from modules.multimodal.upstream import (
    EMPTY_RESPONSE_DISCONNECT,
    UpstreamRequestError,
    UpstreamTimeoutError,
    request_bytes,
    stream_upstream,
)


PAYLOAD = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
BODY = b'{"messages":[{"role":"user","content":"hello"}],"stream":true}'
HEADERS = {"content-type": "application/json", "x-mm-bridge-request-id": "test-request"}
URL = "http://model.test:8000/v1/messages?beta=true"


class BodyStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(PAYLOAD,), error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closed = True


def response(status=200, stream=None):
    return httpx.Response(
        status,
        headers={"content-type": "text/event-stream"},
        stream=stream if stream is not None else BodyStream(),
    )


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logger = logging.getLogger("mm_bridge")
        self.enterContext(patch.object(logger, "handlers", [logging.NullHandler()]))
        self.enterContext(patch.object(logger, "propagate", False))

    async def client(self, actions):
        calls = []

        async def handle(request):
            calls.append(request)
            action = actions[len(calls) - 1]
            if isinstance(action, BaseException):
                raise action
            return action

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        return client, calls

    async def stream(self, client, *, method="POST", url=URL):
        return await stream_upstream(
            client, method, url, HEADERS, BODY,
            "vllm_direct", "test-request", heartbeat_seconds=0,
        )

    async def buffered(self, client, *, url=URL, stage="vllm_final"):
        return await request_bytes(
            client, "POST", url, HEADERS, BODY,
            stage=stage, request_id="test-request",
        )

    async def test_stream_connection_failure_retries_once_with_identical_request(self):
        for error in (
            httpx.ConnectError("All connection attempts failed"),
            httpx.RemoteProtocolError(EMPTY_RESPONSE_DISCONNECT),
        ):
            with self.subTest(error=type(error).__name__):
                client, calls = await self.client([error, response()])
                with self.assertLogs("mm_bridge", level="INFO") as logs:
                    result = await self.stream(client)
                    data = b"".join([chunk async for chunk in result.body_iterator])
                self.assertEqual(data, PAYLOAD)
                self.assertEqual(len(calls), 2)
                self.assertIsNot(calls[0], calls[1])
                self.assertEqual(calls[0].url, calls[1].url)
                self.assertEqual(calls[0].headers, calls[1].headers)
                self.assertEqual(calls[0].content, calls[1].content)
                self.assertEqual(calls[1].content, BODY)
                self.assertEqual(result.headers["x-mm-bridge-request-id"], "test-request")
                self.assertIn("upstream_connection_recovered", "\n".join(logs.output))

    async def test_buffered_generation_and_analyzer_recover(self):
        for path in ("/v1/chat/completions", "/v1/messages/", "/v1/responses"):
            for stage in ("vllm_final", "llama_analyzer"):
                with self.subTest(path=path, stage=stage):
                    body_stream = BodyStream(chunks=(b'{"ok":true}',))
                    client, calls = await self.client([
                        httpx.RemoteProtocolError(EMPTY_RESPONSE_DISCONNECT),
                        response(stream=body_stream),
                    ])
                    result = await self.buffered(client, url="http://model.test" + path, stage=stage)
                    self.assertEqual(result.body, b'{"ok":true}')
                    self.assertEqual(len(calls), 2)
                    self.assertTrue(body_stream.closed)

    async def test_repeated_connection_failure_returns_502_through_app(self):
        for streaming in (False, True):
            with self.subTest(stream=streaming):
                client, calls = await self.client([
                    httpx.ConnectError("first failure"),
                    httpx.ConnectError("second failure"),
                ])
                with patch.object(bridge_app.httpx, "AsyncClient", return_value=client):
                    app = bridge_app.create_app(Settings(bridge_mode=2, vllm_model="test-model"))
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://bridge.test"
                ) as incoming:
                    result = await incoming.post(
                        "/v1/messages?beta=true",
                        headers={"x-request-id": "test-request"},
                        json={"messages": [{"role": "user", "content": "hi"}], "stream": streaming},
                    )
                self.assertEqual(len(calls), 2)
                self.assertEqual(result.status_code, 502)
                self.assertEqual(result.json()["error"]["transport_error"], "ConnectError")
                self.assertEqual(result.headers["x-mm-bridge-stage"], "vllm_direct")
                self.assertEqual(result.headers["x-mm-bridge-request-id"], "test-request")

    async def test_only_the_two_selected_errors_are_retried(self):
        for error in (
            httpx.ConnectTimeout("connect timeout"),
            httpx.ReadTimeout("read timeout"),
            httpx.ReadError("read failed"),
            httpx.WriteError("write failed"),
            httpx.RemoteProtocolError("illegal status line"),
        ):
            with self.subTest(error=type(error).__name__):
                client, calls = await self.client([error])
                with self.assertRaises(type(error)):
                    await self.stream(client)
                self.assertEqual(len(calls), 1)

    async def test_other_post_and_mutating_methods_are_not_retried(self):
        for method, path in (
            ("POST", "/v1/files"),
            ("POST", "/v1/batches"),
            ("PUT", "/v1/messages"),
            ("DELETE", "/v1/files/123"),
        ):
            with self.subTest(method=method, path=path):
                client, calls = await self.client([httpx.ConnectError("failed")])
                with self.assertRaises(httpx.ConnectError):
                    await self.stream(client, method=method, url="http://model.test" + path)
                self.assertEqual(len(calls), 1)

    async def test_model_lookup_can_retry(self):
        client, calls = await self.client([httpx.ConnectError("failed"), response()])
        result = await request_bytes(
            client, "GET", "http://model.test/v1/models", HEADERS, b"",
            stage="vllm_model_discovery", request_id="test-request",
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(calls), 2)

    async def test_http_error_responses_are_forwarded_without_retry(self):
        for status in (400, 401, 429, 500, 502, 503):
            with self.subTest(status=status):
                client, calls = await self.client([response(status)])
                result = await self.stream(client)
                self.assertEqual(result.status_code, status)
                self.assertEqual(b"".join([c async for c in result.body_iterator]), PAYLOAD)
                self.assertEqual(len(calls), 1)

    async def test_stream_body_failure_never_replays_even_before_first_chunk(self):
        for chunks in ((), (b"data: partial\n\n",)):
            with self.subTest(chunks=chunks):
                body_stream = BodyStream(chunks, httpx.RemoteProtocolError(EMPTY_RESPONSE_DISCONNECT))
                client, calls = await self.client([response(stream=body_stream)])
                result = await self.stream(client)
                seen = []
                with self.assertRaises(httpx.RemoteProtocolError):
                    async for chunk in result.body_iterator:
                        seen.append(chunk)
                self.assertEqual(seen, list(chunks))
                self.assertEqual(len(calls), 1)
                self.assertTrue(body_stream.closed)

    async def test_buffered_body_failure_never_replays(self):
        body_stream = BodyStream((b"partial",), httpx.RemoteProtocolError(EMPTY_RESPONSE_DISCONNECT))
        client, calls = await self.client([response(stream=body_stream)])
        with self.assertRaises(UpstreamRequestError) as error:
            await self.buffered(client)
        self.assertEqual(error.exception.error_type, "RemoteProtocolError")
        self.assertEqual(len(calls), 1)
        self.assertTrue(body_stream.closed)

    async def test_headers_already_received_prevent_retry(self):
        calls = []

        async def handle(request):
            calls.append(request)
            await request.extensions["trace"]("http11.receive_response_headers.complete", {})
            raise httpx.RemoteProtocolError(EMPTY_RESPONSE_DISCONNECT)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(httpx.RemoteProtocolError):
                await self.stream(client)
        self.assertEqual(len(calls), 1)

    async def test_cancellation_does_not_retry(self):
        client, calls = await self.client([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await self.stream(client)
        self.assertEqual(len(calls), 1)

    async def test_cancellation_during_backoff_does_not_retry(self):
        client, calls = await self.client([httpx.ConnectError("failed")])
        with patch("modules.multimodal.upstream.asyncio.sleep", side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await self.stream(client)
        self.assertEqual(len(calls), 1)

    async def test_buffered_timeout_mapping_is_preserved(self):
        client, calls = await self.client([httpx.ReadTimeout("timeout")])
        with self.assertRaises(UpstreamTimeoutError) as error:
            await self.buffered(client)
        self.assertEqual(error.exception.stage, "vllm_final")
        self.assertEqual(error.exception.request_id, "test-request")
        self.assertEqual(len(calls), 1)

    async def test_failure_log_includes_os_code_but_not_credentials_or_body(self):
        cause = OSError(111, "PRIVATE_ERROR_TEXT")
        cause.winerror = 10061
        error = httpx.ConnectError("PRIVATE_ERROR_TEXT")
        error.__cause__ = cause
        client, calls = await self.client([error, error])
        with self.assertLogs("mm_bridge", level="INFO") as logs:
            with self.assertRaises(httpx.ConnectError):
                await self.stream(client, url="http://user:PRIVATE_PASSWORD@model.test/v1/messages?key=PRIVATE_QUERY")
        output = "\n".join(logs.output)
        self.assertIn("target_host=model.test target_port=80", output)
        self.assertIn("winerror=10061", output)
        self.assertIn("attempt=2", output)
        self.assertNotIn("PRIVATE_", output)
        self.assertNotIn(BODY.decode(), output)
        self.assertEqual(len(calls), 2)

    async def test_keepalive_setting_does_not_change_request_timeout(self):
        with patch.dict(os.environ, {"MM_HTTP_TIMEOUT_SECONDS": "3600", "MM_HTTP_KEEPALIVE_EXPIRY_SECONDS": "2"}):
            settings = Settings.from_env()
        async with httpx.AsyncClient() as client:
            with patch.object(bridge_app.httpx, "AsyncClient", return_value=client) as factory:
                bridge_app.create_app(Settings(
                    bridge_mode=2,
                    http_timeout_seconds=settings.http_timeout_seconds,
                    http_keepalive_expiry_seconds=settings.http_keepalive_expiry_seconds,
                ))
        self.assertEqual(factory.call_args.kwargs["timeout"].read, 3600)
        self.assertEqual(factory.call_args.kwargs["timeout"].connect, 30)
        self.assertEqual(factory.call_args.kwargs["limits"].keepalive_expiry, 2)
        self.assertEqual(factory.call_args.kwargs["limits"].max_connections, 100)
        self.assertEqual(factory.call_args.kwargs["limits"].max_keepalive_connections, 20)


class WireStream:
    """Network seam below real HTTPX/httpcore HTTP/1.1 parsing and pooling."""

    def __init__(self, backend, number):
        self.backend = backend
        self.number = number
        self.requests = 0
        self.closed = False

    async def write(self, buffer, timeout=None):
        if buffer.startswith(b"POST "):
            self.requests += 1
            self.backend.writes.append((self.number, self.requests))

    async def read(self, max_bytes, timeout=None):
        if self.backend.close_on_reuse and self.number == 1 and self.requests == 2:
            return b""
        return (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            + f"Content-Length: {len(PAYLOAD)}\r\n\r\n".encode()
            + PAYLOAD
        )

    async def aclose(self):
        self.closed = True

    def get_extra_info(self, name):
        # Simulate a socket still alive at the pool's pre-request check.
        return False if name == "is_readable" else None


class WireBackend:
    def __init__(self, *, close_on_reuse=False, fail_connect_on=()):
        self.close_on_reuse = close_on_reuse
        self.fail_connect_on = fail_connect_on
        self.connects = 0
        self.streams = []
        self.writes = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connects += 1
        if self.connects in self.fail_connect_on:
            raise httpcore.ConnectError("All connection attempts failed") from OSError(111, "connection refused")
        stream = WireStream(self, len(self.streams) + 1)
        self.streams.append(stream)
        return stream

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class WireRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, backend):
        transport = httpx.AsyncHTTPTransport(
            limits=httpx.Limits(keepalive_expiry=2), trust_env=False,
        )
        transport._pool._network_backend = backend
        client = httpx.AsyncClient(transport=transport, timeout=3600, trust_env=False)
        self.addAsyncCleanup(client.aclose)
        return client

    async def stream(self, client):
        return await stream_upstream(
            client, "POST", URL, HEADERS, BODY,
            "vllm_direct", "test-request", heartbeat_seconds=0,
        )

    async def consume(self, response):
        self.assertEqual(b"".join([chunk async for chunk in response.body_iterator]), PAYLOAD)

    async def test_reused_socket_eof_is_recovered_on_new_connection(self):
        backend = WireBackend(close_on_reuse=True)
        client = await self.client(backend)
        with self.assertLogs("mm_bridge", level="INFO") as logs:
            await self.consume(await self.stream(client))
            await self.consume(await self.stream(client))
        self.assertEqual(backend.writes, [(1, 1), (1, 2), (2, 1)])
        self.assertTrue(backend.streams[0].closed)
        output = "\n".join(logs.output)
        self.assertIn("attempt=1 connection=reused phase=response_headers", output)
        self.assertIn("attempt=2 connection=new", output)

    async def test_new_tcp_connection_failure_recovers_and_logs_os_cause(self):
        backend = WireBackend(fail_connect_on=(1,))
        client = await self.client(backend)
        with self.assertLogs("mm_bridge", level="INFO") as logs:
            await self.consume(await self.stream(client))
        self.assertEqual(backend.connects, 2)
        self.assertEqual(backend.writes, [(1, 1)])
        output = "\n".join(logs.output)
        self.assertIn("connection=new phase=connect", output)
        self.assertIn("errno=111", output)

    async def test_recovery_keeps_other_active_responses_open(self):
        backend = WireBackend(fail_connect_on=(2,))
        client = await self.client(backend)
        with self.assertLogs("mm_bridge", level="INFO"):
            active = await self.stream(client)
            await self.consume(await self.stream(client))
        self.assertFalse(backend.streams[0].closed)
        await self.consume(active)
        self.assertEqual(backend.connects, 3)


class SettingsTests(unittest.TestCase):
    def test_keepalive_default_and_invalid_values(self):
        self.assertEqual(Settings().http_keepalive_expiry_seconds, 2)
        self.assertEqual(Settings(http_keepalive_expiry_seconds=0).http_keepalive_expiry_seconds, 0)
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(http_keepalive_expiry_seconds=value)


if __name__ == "__main__":
    unittest.main()
