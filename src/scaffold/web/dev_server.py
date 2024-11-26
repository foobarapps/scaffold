"""
A simple and easy to understand ASGI dev server built to make debugging easy and for education purposes.
It supports HTTP/1.1 and WebSockets.
"""

import argparse
import asyncio
import copy
import importlib
import os
import signal
import socket
import sys
import urllib.parse
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import h11
from watchfiles import awatch
from wsproto import WSConnection
from wsproto.connection import ConnectionType
from wsproto.events import (
    AcceptConnection,
    BytesMessage,
    CloseConnection,
    Ping,
    Request,
    TextMessage,
)

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


async def handle_http(
    app: ASGIApp,
    state: LifespanState,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    conn: h11.Connection,
    http_request: h11.Request,
    host: str,
    port: int,
) -> None:
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

    finished_sending_data = asyncio.Event()

    async def receive() -> ASGIReceiveEvent:
        while True:
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
                            h11.InformationalResponse(status_code=10, headers=[]),
                        ),
                    )
                data = await reader.read(1024)
                conn.receive_data(data)

            else:
                await finished_sending_data.wait()
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
                finished_sending_data.set()

        await writer.drain()

    await app(scope, receive, send)


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

        while True:
            try:
                for event in ws_conn.events():
                    if isinstance(event, Request):
                        return {
                            "type": "websocket.connect",
                        }

                    if isinstance(event, Ping):
                        writer.write(ws_conn.send(event.response()))
                        await writer.drain()

                    if isinstance(event, TextMessage):
                        text_message_content += event.data
                        if event.message_finished:
                            return {
                                "type": "websocket.receive",
                                "text": text_message_content,
                            }

                    if isinstance(event, BytesMessage):
                        bytes_message_content += event.data
                        if event.message_finished:
                            return {
                                "type": "websocket.receive",
                                "bytes": bytes_message_content,
                            }

                data = await reader.read(1024)
                if not data:
                    break
                ws_conn.receive_data(data)

            except Exception as e:  # noqa: BLE001
                print(f"WebSocket error: {e}")
                break

        return {
            "type": "websocket.disconnect",
            # default value as per https://asgi.readthedocs.io/en/latest/specs/www.html#disconnect-receive-event-ws
            # TODO check if we received the code from the client
            "code": 1005,
        }

    async def send(event: ASGISendEvent) -> None:
        response = None

        # TODO check that all events are handled

        if event["type"] == "websocket.accept":
            response = ws_conn.send(AcceptConnection())

        if event["type"] == "websocket.send":
            if "bytes" in event and event["bytes"]:
                response = ws_conn.send(BytesMessage(data=event["bytes"]))
            elif "text" in event and event["text"]:
                response = ws_conn.send(TextMessage(data=event["text"]))

        if event["type"] == "websocket.close":
            response = ws_conn.send(
                CloseConnection(
                    code=event.get("code", 1000),
                    reason=event.get("reason"),
                ),
            )

        if response:
            writer.write(response)
            await writer.drain()

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

        # Handle HTTP requests and switching to WebSocket
        # Handle keep-alive connections
        while True:
            data = await reader.read(1024)
            if not data:  # Connection closed by client
                break
            conn.receive_data(data)

            while True:
                event = conn.next_event()

                if event is h11.NEED_DATA:
                    break

                if isinstance(event, h11.ConnectionClosed):
                    break

                if event is h11.PAUSED:
                    conn.start_next_cycle()

                if isinstance(event, h11.Request):
                    http_request = event

                    headers = dict(event.headers)
                    if (
                        headers.get(b"connection", b"").lower() == b"upgrade"
                        and headers.get(b"upgrade", b"").lower() == b"websocket"
                    ):
                        is_websocket_request = True
                        break

                    await handle_http(
                        app,
                        state,
                        reader,
                        writer,
                        conn,
                        http_request,
                        host,
                        port,
                    )

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
        writer.close()
        await writer.wait_closed()
        # client_socket.close()


@asynccontextmanager
async def lifespan(
    app: ASGIApp,
    state: LifespanState,
) -> AsyncGenerator:
    scope: LifespanScope = {
        "type": "lifespan",
        "asgi": {"version": "3.0"},
        "state": state,
    }

    startup_complete = asyncio.Event()
    shutdown_started = asyncio.Event()
    shutdown_complete = asyncio.Event()

    async def receive() -> ASGIReceiveEvent:
        if not startup_complete.is_set():
            return {"type": "lifespan.startup"}
        await shutdown_started.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message: ASGISendEvent) -> None:
        if message["type"] == "lifespan.startup.complete":
            startup_complete.set()

        elif message["type"] == "lifespan.shutdown.complete":
            shutdown_complete.set()

    # Start lifespan
    lifespan_task: asyncio.Task = asyncio.create_task(app(scope, receive, send))

    startup_complete_wait_task = asyncio.create_task(startup_complete.wait())

    # When one of the tasks completes, it means that the ASGI app either
    # 1) supports the lifespan protocol and the startup is complete or
    # 2) does not support the lifespan protocol (and did not call the receive function)
    await asyncio.wait(
        [lifespan_task, startup_complete_wait_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    yield

    shutdown_started.set()
    await shutdown_complete.wait()


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
        async for _ in awatch(""):
            print("Detected file changes. Triggering restart.")
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
