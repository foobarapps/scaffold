from collections.abc import Sequence
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession

from .repository import GenericSqlRepository


class GenericSqlUnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> None:
        return

    async def __aexit__(
        self,
        exc_type: type,
        exc: BaseException,
        tb: TracebackType,
    ) -> None:
        await self.rollback()
        await self._session.close()

    async def commit(self) -> None:
        for repo in self._repositories:
            await repo.sync_state()

        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    @property
    def _repositories(self) -> Sequence[GenericSqlRepository]:
        return [value for value in self.__dict__.values() if isinstance(value, GenericSqlRepository)]
