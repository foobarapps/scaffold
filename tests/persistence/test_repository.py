import dataclasses
from unittest.mock import AsyncMock, Mock

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


@dataclasses.dataclass
class UnhashableEntity:
    id: int
    name: str


class UnhashableMapper:
    def map_persistence_to_domain(self, dto: ExampleDTO) -> UnhashableEntity:
        return UnhashableEntity(id=dto.id, name=dto.name)

    def map_domain_to_persistence(self, entity: UnhashableEntity) -> ExampleDTO:
        return ExampleDTO(id=entity.id, name=entity.name)


class UnhashableRepository(GenericSqlRepository[UnhashableEntity, ExampleDTO]):
    mapper = UnhashableMapper()

    def track_persisted(self, dto: ExampleDTO) -> UnhashableEntity:
        return self._map_persistence_to_domain(dto)


@pytest.mark.asyncio
async def test_entity_that_cannot_be_hashed_is_tracked_as_the_object_it_is() -> None:
    session = AsyncMock()
    session.add = Mock()
    repo = UnhashableRepository(BaseSqlUnitOfWork(session))
    loaded = repo.track_persisted(ExampleDTO(id=1, name="old"))
    new = UnhashableEntity(id=2, name="new")
    repo.add(new)

    loaded.name = "changed"
    await repo.flush()

    session.add.assert_called_once_with(ExampleDTO(id=2, name="new"))
    session.merge.assert_awaited_once_with(ExampleDTO(id=1, name="changed"))
    with pytest.raises(ValueError, match="already tracked"):
        repo.add(loaded)


class AuditedRepository(ExampleRepository):
    def __init__(self, uow: BaseSqlUnitOfWork) -> None:
        super().__init__(uow)
        self.added: list[ExampleEntity] = []

    def _map_tracked_domain_to_persistence(self, entity: ExampleEntity, persistence_obj: ExampleDTO) -> ExampleDTO:
        # Keeps the name the row was loaded with
        return ExampleDTO(id=entity.id, name=persistence_obj.name)

    async def _after_flush(self, new_entities: list[ExampleEntity]) -> None:
        self.added += new_entities


@pytest.mark.asyncio
async def test_repository_can_hook_into_mapping_and_flushing() -> None:
    session = AsyncMock()
    session.add = Mock()
    repo = AuditedRepository(BaseSqlUnitOfWork(session))
    loaded = repo.track_persisted(ExampleDTO(id=1, name="original"))
    loaded.name = "changed"
    new = ExampleEntity(id=2, name="new")
    repo.add(new)

    await repo.flush()

    session.merge.assert_awaited_once_with(ExampleDTO(id=1, name="original"))
    assert repo.added == [new]


@pytest.mark.asyncio
async def test_second_flush_updates_an_entity_the_first_one_added() -> None:
    session = AsyncMock()
    session.add = Mock()
    repo = ExampleRepository(BaseSqlUnitOfWork(session))
    entity = ExampleEntity(id=1, name="new")
    repo.add(entity)

    await repo.flush()
    entity.name = "changed"
    await repo.flush()

    session.add.assert_called_once_with(ExampleDTO(id=1, name="new"))
    session.merge.assert_awaited_once_with(ExampleDTO(id=1, name="changed"))
