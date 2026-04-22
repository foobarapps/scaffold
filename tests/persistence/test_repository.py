import dataclasses
from unittest.mock import AsyncMock

import pytest

from scaffold.persistence.repository import GenericSqlRepository
from scaffold.persistence.uow import BaseSqlUnitOfWork, UnitOfWorkClosedError


@dataclasses.dataclass(eq=False)
class ExampleEntity:
    id: int
    name: str


@dataclasses.dataclass(frozen=True)
class ExampleDTO:
    id: int
    name: str


class ExampleMapper:
    def map_persistence_to_domain(self, dto: ExampleDTO) -> ExampleEntity:
        return ExampleEntity(id=dto.id, name=dto.name)

    def map_domain_to_persistence(self, entity: ExampleEntity) -> ExampleDTO:
        return ExampleDTO(id=entity.id, name=entity.name)


class ExampleRepository(GenericSqlRepository[ExampleEntity, ExampleDTO]):
    mapper = ExampleMapper()

    def track_persisted(self, dto: ExampleDTO) -> ExampleEntity:
        return self._map_persistence_to_domain(dto)


@pytest.mark.asyncio
async def test_add_rejects_tracked_persisted_entity() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)
    repo = ExampleRepository(uow)
    entity = repo.track_persisted(ExampleDTO(id=1, name="tracked"))

    with pytest.raises(ValueError, match="already tracked as persisted"):
        repo.add(entity)


@pytest.mark.asyncio
async def test_remove_discards_new_entity_without_flushing() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)
    repo = ExampleRepository(uow)
    entity = ExampleEntity(id=1, name="new")

    repo.add(entity)
    await repo.remove(entity)
    await repo.flush()

    session.add.assert_not_called()
    session.merge.assert_not_awaited()
    session.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_flush_deletes_tracked_entity_via_identity_map() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)
    repo = ExampleRepository(uow)
    dto = ExampleDTO(id=1, name="tracked")
    entity = repo.track_persisted(dto)

    await repo.remove(entity)
    await repo.flush()

    session.add.assert_not_called()
    session.merge.assert_not_awaited()
    session.delete.assert_awaited_once_with(dto)


@pytest.mark.asyncio
async def test_flush_merges_tracked_entity_updates() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)
    repo = ExampleRepository(uow)
    entity = repo.track_persisted(ExampleDTO(id=1, name="before"))
    entity.name = "after"

    await repo.flush()

    session.add.assert_not_called()
    session.merge.assert_awaited_once_with(ExampleDTO(id=1, name="after"))
    session.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_mutating_entity_after_commit_does_not_persist_again() -> None:
    session = AsyncMock()
    uow = BaseSqlUnitOfWork(session)
    repo = ExampleRepository(uow)
    entity = repo.track_persisted(ExampleDTO(id=1, name="before"))

    await uow.commit()

    entity.name = "after"

    with pytest.raises(UnitOfWorkClosedError, match="closed Unit of Work"):
        await uow.commit()

    session.merge.assert_awaited_once_with(ExampleDTO(id=1, name="before"))
