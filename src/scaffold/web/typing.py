from collections.abc import Awaitable, Callable, Coroutine, Iterable
from typing import Any, Literal, NotRequired, TypedDict


class ASGIVersions(TypedDict, total=False):
    spec_version: str
    version: Literal["2.0"] | Literal["3.0"]


type LifespanState = dict[str, Any]

type Headers = list[tuple[bytes, bytes]]


class HTTPScope(TypedDict):
    type: Literal["http"]
    asgi: ASGIVersions
    http_version: str
    method: str
    scheme: NotRequired[str]
    path: str
    raw_path: NotRequired[bytes]
    query_string: bytes
    root_path: NotRequired[str]
    headers: Headers
    client: NotRequired[tuple[str, int]]
    server: NotRequired[tuple[str, int | None]]
    state: NotRequired[LifespanState]


class WebsocketScope(TypedDict):
    type: Literal["websocket"]
    asgi: ASGIVersions
    http_version: NotRequired[str]
    scheme: NotRequired[str]
    path: str
    raw_path: NotRequired[bytes]
    query_string: NotRequired[bytes]
    root_path: NotRequired[str]
    headers: Headers
    client: NotRequired[tuple[str, int]]
    server: NotRequired[tuple[str, int | None]]
    subprotocols: NotRequired[Iterable[str]]
    state: NotRequired[LifespanState]


class LifespanScope(TypedDict):
    type: Literal["lifespan"]
    asgi: ASGIVersions
    state: LifespanState


type Scope = HTTPScope | WebsocketScope | LifespanScope


class HTTPRequestEvent(TypedDict):
    type: Literal["http.request"]
    body: bytes
    more_body: bool


class HTTPResponseStartEvent(TypedDict):
    type: Literal["http.response.start"]
    status: int
    headers: Headers
    trailers: NotRequired[bool]


class HTTPResponseBodyEvent(TypedDict):
    type: Literal["http.response.body"]
    body: bytes
    more_body: bool


class HTTPResponseTrailersEvent(TypedDict):
    type: Literal["http.response.trailers"]
    headers: Headers
    more_trailers: NotRequired[bool]


class HTTPServerPushEvent(TypedDict):
    type: Literal["http.response.push"]
    path: str
    headers: Headers


class HTTPEarlyHintEvent(TypedDict):
    type: Literal["http.response.early_hint"]
    links: Iterable[bytes]


class HTTPDisconnectEvent(TypedDict):
    type: Literal["http.disconnect"]


class WebsocketConnectEvent(TypedDict):
    type: Literal["websocket.connect"]


class WebsocketAcceptEvent(TypedDict):
    type: Literal["websocket.accept"]
    subprotocol: str | None
    headers: Headers


class WebsocketReceiveEvent(TypedDict):
    type: Literal["websocket.receive"]
    bytes: NotRequired[bytes]
    text: NotRequired[str]


class WebsocketSendEvent(TypedDict):
    type: Literal["websocket.send"]
    bytes: bytes | None
    text: str | None


class WebsocketResponseStartEvent(TypedDict):
    type: Literal["websocket.http.response.start"]
    status: int
    headers: Headers


class WebsocketResponseBodyEvent(TypedDict):
    type: Literal["websocket.http.response.body"]
    body: bytes
    more_body: bool


class WebsocketDisconnectEvent(TypedDict):
    type: Literal["websocket.disconnect"]
    code: int


class WebsocketCloseEvent(TypedDict):
    type: Literal["websocket.close"]
    code: NotRequired[int]
    reason: NotRequired[str]


class LifespanStartupEvent(TypedDict):
    type: Literal["lifespan.startup"]


class LifespanShutdownEvent(TypedDict):
    type: Literal["lifespan.shutdown"]


class LifespanStartupCompleteEvent(TypedDict):
    type: Literal["lifespan.startup.complete"]


class LifespanStartupFailedEvent(TypedDict):
    type: Literal["lifespan.startup.failed"]
    message: str


class LifespanShutdownCompleteEvent(TypedDict):
    type: Literal["lifespan.shutdown.complete"]


class LifespanShutdownFailedEvent(TypedDict):
    type: Literal["lifespan.shutdown.failed"]
    message: str


type ASGIReceiveEvent = HTTPRequestEvent | HTTPDisconnectEvent | WebsocketConnectEvent | WebsocketReceiveEvent | WebsocketDisconnectEvent | LifespanStartupEvent | LifespanShutdownEvent

type ASGISendEvent = HTTPResponseStartEvent | HTTPResponseBodyEvent | HTTPResponseTrailersEvent | HTTPServerPushEvent | HTTPEarlyHintEvent | HTTPDisconnectEvent | WebsocketAcceptEvent | WebsocketSendEvent | WebsocketResponseStartEvent | WebsocketResponseBodyEvent | WebsocketCloseEvent | LifespanStartupCompleteEvent | LifespanStartupFailedEvent | LifespanShutdownCompleteEvent | LifespanShutdownFailedEvent

type ASGIReceiveCallable = Callable[[], Awaitable[ASGIReceiveEvent]]
type ASGISendCallable = Callable[[ASGISendEvent], Awaitable[None]]

type ASGIApp = Callable[
    [
        Scope,
        ASGIReceiveCallable,
        ASGISendCallable,
    ],
    Coroutine[Any, Any, None],
]
