"""
A simple and easy to understand ASGI dev server built to make debugging easy and for education purposes.
It supports HTTP/1.1 and WebSockets.
"""

import argparse
import asyncio
import copy
import errno
import importlib
import os
import signal
import socket
import sys
import urllib.parse
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import Any

import h11
from watchfiles import awatch  # type: ignore[import-untyped]
from wsproto import WSConnection
from wsproto.connection import ConnectionState, ConnectionType
from wsproto.events import (
    AcceptConnection,
    BytesMessage,
    CloseConnection,
    Ping,
    Request,
    TextMessage,
)
from wsproto.utilities import ProtocolError

from .typing import (
    ASGIApp,
    ASGIReceiveEvent,
    ASGISendEvent,
    HTTPScope,
    LifespanScope,
    LifespanState,
    WebsocketScope,
)


def write(writer: asyncio.StreamWriter, data: bytes | None) -> None:
    if data:
        writer.write(data)


def parse_connection_tokens(header_value: bytes) -> set[bytes]:
    return {
        token.strip().lower() for token in header_value.split(b",") if token.strip()
    }


PEER_DISCONNECT_ERRNOS = {
    errno.EPIPE,
    errno.ECONNABORTED,
    errno.ECONNRESET,
    errno.ENOTCONN,
}


def is_peer_disconnect_error(exc: OSError) -> bool:
    return exc.errno in PEER_DISCONNECT_ERRNOS


async def read_from_peer(reader: asyncio.StreamReader, size: int = 1024) -> bytes:
    try:
        return await reader.read(size)
    except OSError as exc:
        if is_peer_disconnect_error(exc):
            return b""
        raise


async def handle_http(
    app: ASGIApp,
    state: LifespanState,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    conn: h11.Connection,
    http_request: h11.Request,
    host: str,
    port: int,
) -> bool:
    print(f"{http_request.method.decode()} {http_request.target.decode()}")

    parsed_target = urllib.parse.urlparse(http_request.target.decode())

    scope: HTTPScope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": http_request.http_version.decode(),
        "scheme": "http",
        "method": http_request.method.decode(),
        "path": parsed_target.path,
        "query_string": parsed_target.query.encode(),
        "headers": [(name.lower(), value) for name, value in http_request.headers],
        "server": (host, port),
        "state": state,
    }
    response_complete = asyncio.Event()
    keep_connection_open = True

    async def receive() -> ASGIReceiveEvent:
        nonlocal keep_connection_open
        while True:
            if response_complete.is_set():
                return {"type": "http.disconnect"}

            event = conn.next_event()

            if isinstance(event, h11.Data):
                return {
                    "type": "http.request",
                    "body": event.data,
                    "more_body": True,
                }

            if isinstance(event, h11.EndOfMessage):
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }

            if event is h11.NEED_DATA:
                if conn.they_are_waiting_for_100_continue:
                    write(
                        writer,
                        conn.send(
                            h11.InformationalResponse(status_code=100, headers=[]),
                        ),
                    )
                data = await read_from_peer(reader)
                if not data:
                    keep_connection_open = False
                    return {"type": "http.disconnect"}
                try:
                    conn.receive_data(data)
                except h11.RemoteProtocolError:
                    keep_connection_open = False
                    return {"type": "http.disconnect"}
                continue

            keep_connection_open = False
            return {"type": "http.disconnect"}

    async def send(message: ASGISendEvent) -> None:
        if message["type"] == "http.response.start":
            response = h11.Response(
                status_code=message["status"],
                headers=message["headers"],
            )
            write(writer, conn.send(response))

        elif message["type"] == "http.response.body":
            write(writer, (conn.send(h11.Data(data=message["body"]))))

            if not message.get("more_body", False):
                write(writer, (conn.send(h11.EndOfMessage())))
                response_complete.set()

        try:
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected so stop trying to send data
            pass

    await app(scope, receive, send)
    return keep_connection_open


async def handle_websockets(
    app: ASGIApp,
    state: LifespanState,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    http_request: h11.Request,
    host: str,
    port: int,
) -> None:
    ws_conn = WSConnection(ConnectionType.SERVER)
    ws_conn.initiate_upgrade_connection(list(http_request.headers), http_request.target)

    parsed_target = urllib.parse.urlparse(http_request.target.decode())

    scope: WebsocketScope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "scheme": "ws",
        "path": parsed_target.path,
        "query_string": parsed_target.query.encode(),
        "headers": [(name.lower(), value) for name, value in http_request.headers],
        "server": (host, port),
        "state": state,
    }

    async def receive() -> ASGIReceiveEvent:
        text_message_content = ""
        bytes_message_content = b""
        close_code = 1005  # Default: No Status Received

        while True:
            try:
                for event in ws_conn.events():
                    if isinstance(event, Request):
                        return {
                            "type": "websocket.connect",
                        }

                    if isinstance(event, Ping):
                        try:
                            writer.write(ws_conn.send(event.response()))
                            await writer.drain()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                        continue

                    if isinstance(event, CloseConnection):
                        close_code = getattr(event, "code", 1005)
                        reason = getattr(event, "reason", None)
                        response = ws_conn.send(
                            CloseConnection(code=close_code, reason=reason),
                        )
                        if response:
                            try:
                                writer.write(response)
                                await writer.drain()
                            except (BrokenPipeError, ConnectionResetError):
                                pass
                        return {
                            "type": "websocket.disconnect",
                            "code": close_code,
                        }

                    if isinstance(event, TextMessage):
                        text_message_content += event.data
                        if event.message_finished:
                            message_text = text_message_content
                            text_message_content = ""
                            return {
                                "type": "websocket.receive",
                                "text": message_text,
                            }

                    if isinstance(event, BytesMessage):
                        bytes_message_content += event.data
                        if event.message_finished:
                            message_bytes = bytes_message_content
                            bytes_message_content = b""
                            return {
                                "type": "websocket.receive",
                                "bytes": message_bytes,
                            }

                data = await read_from_peer(reader)
                if not data:
                    break
                ws_conn.receive_data(data)
            except ProtocolError:
                break

        return {
            "type": "websocket.disconnect",
            "code": close_code,
        }

    async def send(event: ASGISendEvent) -> None:
        response = None

        match event["type"]:
            case "websocket.accept":
                response = ws_conn.send(AcceptConnection())

            case "websocket.send":
                if "bytes" in event and event["bytes"] is not None:
                    response = ws_conn.send(BytesMessage(data=event["bytes"]))
                elif "text" in event and event["text"] is not None:
                    response = ws_conn.send(TextMessage(data=event["text"]))

            case "websocket.close":
                if ws_conn.state not in {
                    ConnectionState.REMOTE_CLOSING,
                    ConnectionState.LOCAL_CLOSING,
                    ConnectionState.CLOSED,
                }:
                    response = ws_conn.send(
                        CloseConnection(
                            code=event.get("code", 1000),
                            reason=event.get("reason"),
                        ),
                    )

            case _:
                # Other ASGI send event types are not applicable to WebSocket connections
                pass

        if response:
            try:
                writer.write(response)
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                # Client disconnected so stop trying to send data
                pass

    await app(scope, receive, send)


async def handle_connection(
    # client_socket: socket.socket,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    app: ASGIApp,
    state: LifespanState,
    host: str,
    port: int,
) -> None:
    try:
        conn = h11.Connection(h11.SERVER)

        is_websocket_request = False
        http_request = None
        keep_connection_open = True

        # Handle HTTP requests and switching to WebSocket
        # Handle keep-alive connections
        while True:
            data = await read_from_peer(reader)
            if not data:  # Connection closed by client
                break
            try:
                conn.receive_data(data)
            except h11.RemoteProtocolError:
                break

            while True:
                try:
                    event = conn.next_event()
                except h11.RemoteProtocolError:
                    keep_connection_open = False
                    break

                if event is h11.NEED_DATA:
                    break

                if isinstance(event, h11.ConnectionClosed):
                    break

                if event is h11.PAUSED:
                    conn.start_next_cycle()
                    continue

                if isinstance(event, h11.Data | h11.EndOfMessage):
                    # The app might not consume the whole request body.
                    # Discarding here prevents tight loops on unread body events.
                    continue

                if isinstance(event, h11.Request):
                    http_request = event

                    headers = {name.lower(): value for name, value in event.headers}
                    connection_tokens = parse_connection_tokens(
                        headers.get(b"connection", b""),
                    )
                    if (
                        b"upgrade" in connection_tokens
                        and headers.get(b"upgrade", b"").lower() == b"websocket"
                    ):
                        is_websocket_request = True
                        break

                    keep_connection_open = await handle_http(
                        app,
                        state,
                        reader,
                        writer,
                        conn,
                        http_request,
                        host,
                        port,
                    )
                    if not keep_connection_open:
                        break

            if not keep_connection_open:
                break

            if is_websocket_request:
                break

            if conn.our_state is h11.MUST_CLOSE:
                break

        # Handle WebSocket connection
        if is_websocket_request and http_request:
            await handle_websockets(
                app,
                state,
                reader,
                writer,
                http_request,
                host,
                port,
            )

    finally:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=1)
        except (BrokenPipeError, ConnectionResetError, OSError, TimeoutError):
            # Client disconnected abruptly (this is expected and harmless)
            pass
        # client_socket.close()


@asynccontextmanager
async def lifespan(
    app: ASGIApp,
    state: LifespanState,
) -> AsyncGenerator[None, None]:
    scope: LifespanScope = {
        "type": "lifespan",
        "asgi": {"version": "3.0"},
        "state": state,
    }

    startup_complete = asyncio.Event()
    startup_failed = asyncio.Event()
    shutdown_started = asyncio.Event()
    shutdown_complete = asyncio.Event()
    shutdown_failed = asyncio.Event()
    startup_failed_message = ""
    shutdown_failed_message = ""

    async def receive() -> ASGIReceiveEvent:
        if not startup_complete.is_set() and not startup_failed.is_set():
            return {"type": "lifespan.startup"}
        await shutdown_started.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message: ASGISendEvent) -> None:
        nonlocal shutdown_failed_message, startup_failed_message
        if message["type"] == "lifespan.startup.complete":
            startup_complete.set()

        elif message["type"] == "lifespan.startup.failed":
            startup_failed_message = message["message"]
            startup_failed.set()

        elif message["type"] == "lifespan.shutdown.complete":
            shutdown_complete.set()

        elif message["type"] == "lifespan.shutdown.failed":
            shutdown_failed_message = message["message"]
            shutdown_failed.set()
            shutdown_complete.set()

    lifespan_task: asyncio.Task[None] = asyncio.create_task(app(scope, receive, send))

    async def wait_for_lifespan_signal(*events: asyncio.Event) -> int | None:
        waiters = [asyncio.create_task(event.wait()) for event in events]
        try:
            done, _ = await asyncio.wait(
                [lifespan_task, *waiters],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()

        if lifespan_task in done:
            return None

        for index, waiter in enumerate(waiters):
            if waiter in done:
                return index

        error_message = "Lifespan wait completed without a signal"
        raise RuntimeError(error_message)

    async def cancel_lifespan_task() -> None:
        if not lifespan_task.done():
            lifespan_task.cancel()
            with suppress(asyncio.CancelledError):
                await lifespan_task

    def raise_lifespan_task_exception() -> None:
        if lifespan_task.cancelled():
            raise asyncio.CancelledError

        exception = lifespan_task.exception()
        if exception is not None:
            raise exception

    startup_result = await wait_for_lifespan_signal(startup_complete, startup_failed)

    if startup_failed.is_set():
        await cancel_lifespan_task()
        message = startup_failed_message or "lifespan startup failed"
        raise RuntimeError(message)

    lifespan_supported = startup_result == 0
    if startup_result is None:
        raise_lifespan_task_exception()
        lifespan_supported = False

    body_error: BaseException | None = None
    cancellation_requested = False

    try:
        yield
    except BaseException as exc:  # noqa: BLE001
        body_error = exc
        if isinstance(exc, asyncio.CancelledError):
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
            cancellation_requested = True

    shutdown_error: BaseException | None = None
    if lifespan_supported:
        shutdown_started.set()
        shutdown_result = await wait_for_lifespan_signal(shutdown_complete)

        if shutdown_failed.is_set():
            message = shutdown_failed_message or "lifespan shutdown failed"
            shutdown_error = RuntimeError(message)

        elif shutdown_result is None:
            if not lifespan_task.cancelled():
                exception = lifespan_task.exception()
                if exception is not None:
                    shutdown_error = exception

        elif not lifespan_task.done():
            await lifespan_task

    if shutdown_error:
        raise shutdown_error

    if cancellation_requested:
        raise asyncio.CancelledError

    if body_error:
        raise body_error


async def serve(app: ASGIApp, host: str, port: int) -> None:
    state: dict[str, Any] = {}

    async with lifespan(app, state):
        try:
            async with asyncio.TaskGroup() as tg:
                socket_fd = os.environ.get("SOCKET_FD")

                if socket_fd:
                    sock = socket.fromfd(
                        int(socket_fd),
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                    )
                    sock_host, sock_port = sock.getsockname()
                    assert sock_host == host and sock_port == port
                else:
                    sock = create_socket(host, port)

                server = await asyncio.start_server(
                    lambda reader, writer: tg.create_task(
                        handle_connection(
                            reader,
                            writer,
                            app,
                            copy.copy(state),
                            host,
                            port,
                        ),
                    ),
                    sock=sock,
                )

                print(f"Serving on {host}:{port}")

                async with server:
                    await server.serve_forever()

        except* Exception as eg:  # noqa: BLE001
            raise eg.exceptions[0] from None


def import_app(import_string: str) -> ASGIApp:
    module_name, app_name = import_string.split(":")
    module = importlib.import_module(module_name)
    return getattr(module, app_name)


async def run_server(import_string: str, host: str, port: int) -> None:
    try:
        app = import_app(import_string)
        await serve(app, host, port)

    except asyncio.CancelledError:
        print("Server was terminated...")


def is_run_as_module() -> bool:
    return __spec__ is not None


def get_worker_cmd() -> list[str]:
    python_cmd = [sys.executable, "-Xfrozen_modules=off"]

    if is_run_as_module():
        # The script was run as a module
        module_name = __spec__.name
        cmd = [*python_cmd, "-m", module_name, *sys.argv[1:]]
    else:
        # The script was run as a file
        cmd = [*python_cmd, *sys.argv]

    cmd.append("--no-reload")

    return cmd


def create_socket(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)  # Backlog size for queuing connections
    sock.setblocking(False)
    return sock


async def run_reloader(host: str, port: int) -> None:
    process: asyncio.subprocess.Process | None = None
    shutdown_event = asyncio.Event()

    sock = create_socket(host, port)

    def signal_handler() -> None:
        print("Received signal to stop. Terminating subprocess and exiting.")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, signal_handler)

    async def run_worker_subprocess() -> None:
        nonlocal process

        if process and process.returncode is None:
            process.terminate()
            await process.wait()

        env = os.environ.copy()
        env["SOCKET_FD"] = str(sock.fileno())

        command = get_worker_cmd()
        process = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            pass_fds=(sock.fileno(),),
        )

        await process.wait()

    async def watch_files() -> None:
        async for changes in awatch("."):
            print("Detected file changes. Triggering restart.")
            for change, file_path in changes:
                print("\t", f"{file_path} ({change.name})")
            return

    while not shutdown_event.is_set():
        process_task = asyncio.create_task(run_worker_subprocess())
        watch_task = asyncio.create_task(watch_files())
        shutdown_wait_task = asyncio.create_task(shutdown_event.wait())

        _, pending = await asyncio.wait(
            [process_task, watch_task, shutdown_wait_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

    if process:
        process.terminate()
        await process.wait()

    sock.close()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Development ASGI server with code reloading",
    )
    parser.add_argument(
        "MODULE_APP",
        help="Module and variable name of the ASGI app (e.g., 'myapp:app')",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind the server to",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the server to",
    )
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="Disable auto-reloading",
    )

    try:
        args = parser.parse_args()

    except argparse.ArgumentError:
        parser.print_help()
        sys.exit(2)

    if args.no_reload:
        await run_server(args.MODULE_APP, args.host, args.port)

    else:
        await run_reloader(args.host, args.port)


if __name__ == "__main__":
    asyncio.run(main())
