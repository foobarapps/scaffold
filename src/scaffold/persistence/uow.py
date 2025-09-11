import typing
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession


class SyncableRepository(typing.Protocol):
    def sync(self) -> None: ...
    def clear_identity_map(self) -> None: ...


class BaseSqlUnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repositories: list[SyncableRepository] = []

    async def __aenter__(self) -> None:
        return

    async def __aexit__(
        self,
        exc_type: type,
        exc: BaseException,
        tb: TracebackType,
    ) -> None:
        await self.rollback()
        await self.session.close()

    async def commit(self) -> None:
        # TODO close the UoW after committing so that it can't be used again?

        for repo in self.repositories:
            repo.sync()

        await self.session.commit()

        # Clear identity maps after successful commit
        for repo in self.repositories:
            repo.clear_identity_map()

    async def rollback(self) -> None:
        await self.session.rollback()

    def register_repository(self, repo: SyncableRepository) -> None:
        self.repositories.append(repo)
