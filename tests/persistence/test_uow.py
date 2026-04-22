import dataclasses
from unittest.mock import AsyncMock

import pytest

from scaffold.persistence.uow import BaseSqlUnitOfWork, UnitOfWorkClosedError


@dataclasses.dataclass
class RecordingRepository:
    flushed: int = 0
    cleared: int = 0

    async def flush(self) -> None:
        self.flushed += 1

    def clear_tracking(self) -> None:
        self.cleared += 1


@pytest.mark.asyncio
async def test_commit_flushes_and_clears_tracking() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)
    repo = RecordingRepository()
    uow.register_repository(repo)

    await uow.commit()

    assert repo.flushed == 1
    assert repo.cleared == 1
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollback_clears_tracking() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)
    repo = RecordingRepository()
    uow.register_repository(repo)

    await uow.rollback()

    assert repo.flushed == 0
    assert repo.cleared == 1
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_commit_raises() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)

    await uow.commit()

    with pytest.raises(UnitOfWorkClosedError, match="closed Unit of Work"):
        await uow.commit()


@pytest.mark.asyncio
async def test_rollback_after_commit_raises() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)

    await uow.commit()

    with pytest.raises(UnitOfWorkClosedError, match="closed Unit of Work"):
        await uow.rollback()


@pytest.mark.asyncio
async def test_commit_after_rollback_raises() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)

    await uow.rollback()

    with pytest.raises(UnitOfWorkClosedError, match="closed Unit of Work"):
        await uow.commit()


@pytest.mark.asyncio
async def test_context_exit_rolls_back_only_when_uow_is_active() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)
    repo = RecordingRepository()
    uow.register_repository(repo)

    async with uow:
        pass

    assert repo.cleared == 1
    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_context_exit_after_commit_only_closes_session() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)
    repo = RecordingRepository()
    uow.register_repository(repo)

    async with uow:
        await uow.commit()

    assert repo.flushed == 1
    assert repo.cleared == 1
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    session.close.assert_awaited_once()
