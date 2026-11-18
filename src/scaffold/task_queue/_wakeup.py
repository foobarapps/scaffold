import asyncio
import contextlib


async def wait_for(event: asyncio.Event, seconds: float) -> bool:
    """Wait up to `seconds` and tell whether the event got set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), seconds)
    return event.is_set()


class Wakeup:
    """
    Lets the one LISTEN task wake the idle consumers.

    A consumer remembers the sequence number before it dequeues and waits for it to change,
    so a notification that arrives between an empty dequeue and the wait is not lost.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._sequence = 0

    @property
    def sequence(self) -> int:
        return self._sequence

    async def signal(self) -> None:
        async with self._condition:
            self._sequence += 1
            self._condition.notify_all()

    async def wait(self, seen_sequence: int, seconds: float) -> None:
        async with self._condition:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._sequence != seen_sequence),
                    seconds,
                )
