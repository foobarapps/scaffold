import typing
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession


class UnitOfWorkRepository(typing.Protocol):
    async def flush(self) -> None: ...
    def clear_tracking(self) -> None: ...


class UnitOfWorkClosedError(Exception):
    pass


class BaseSqlUnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repositories: list[UnitOfWorkRepository] = []
        self._closed = False

    async def __aenter__(self) -> typing.Self:
        return self

    async def __aexit__(
        self,
        exc_type: type,
        exc: BaseException,
        tb: TracebackType,
    ) -> None:
        if not self._closed:
            await self.rollback()

        await self.session.close()  # TODO use reset instead?

    async def commit(self) -> None:
        self._ensure_open()

        for repo in self.repositories:
            await repo.flush()

        await self.session.commit()

        for repo in self.repositories:
            repo.clear_tracking()

        self._closed = True

    async def rollback(self) -> None:
        self._ensure_open()

        await self.session.rollback()

        for repo in self.repositories:
            repo.clear_tracking()

        self._closed = True

    def register_repository(self, repo: UnitOfWorkRepository) -> None:
        self.repositories.append(repo)

    def _ensure_open(self) -> None:
        if not self._closed:
            return

        msg = "Cannot use a closed Unit of Work"
        raise UnitOfWorkClosedError(msg)
