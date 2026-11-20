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
async def test_rollback_after_commit_does_nothing() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)

    await uow.commit()
    await uow.rollback()

    session.rollback.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_failed_commit_rolls_back_and_closes_the_unit_of_work() -> None:
    session = AsyncMock()
    session.commit.side_effect = RuntimeError("duplicate key")
    uow = BaseSqlUnitOfWork(session)
    repo = RecordingRepository()
    uow.register_repository(repo)

    with pytest.raises(RuntimeError, match="duplicate key"):
        await uow.commit()

    # Rolled back by the unit of work itself, with or without a context manager around it
    session.rollback.assert_awaited_once()
    assert repo.cleared == 1
    with pytest.raises(UnitOfWorkClosedError):
        await uow.commit()


@pytest.mark.asyncio
async def test_rollback_in_an_error_handler_keeps_the_original_error() -> None:
    session = AsyncMock()
    session.commit.side_effect = RuntimeError("duplicate key")
    uow = BaseSqlUnitOfWork(session)

    async def commit_or_roll_back() -> None:
        try:
            await uow.commit()
        except Exception:
            await uow.rollback()
            raise

    with pytest.raises(RuntimeError, match="duplicate key"):
        await commit_or_roll_back()

    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_failing_rollback_does_not_hide_the_error_of_the_commit() -> None:
    session = AsyncMock()
    session.commit.side_effect = RuntimeError("duplicate key")
    session.rollback.side_effect = ConnectionError("the connection is gone")
    uow = BaseSqlUnitOfWork(session)

    with pytest.raises(RuntimeError, match="duplicate key"):
        await uow.commit()

    with pytest.raises(UnitOfWorkClosedError):
        await uow.commit()
