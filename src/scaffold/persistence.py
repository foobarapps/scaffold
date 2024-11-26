import abc
from collections.abc import Sequence
from types import TracebackType, get_original_bases
from typing import Protocol, final, get_args, override

from sqlalchemy.ext.asyncio import AsyncSession


class EntityId(Protocol):
    @property
    def value(self) -> object: ...


class Entity(Protocol):
    @property
    def id(self) -> EntityId: ...


class GenericSqlRepository[E: Entity, ID: EntityId, DTO](abc.ABC):
    dto_class: type[DTO]

    @override
    def __init_subclass__(cls) -> None:
        # TODO Check that the runtime type of the ID type param is the same as the type hint of E.id.
        # Ideally, we would like to do something like `GenericSqlRepository[ID: EntityId, E: Entity[ID], DTO](abc.ABC)`
        # to check it statically but that's currently not possible.

        cls.dto_class = get_args(get_original_bases(cls)[0])[2]

        return super().__init_subclass__()

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._identity_map: dict[EntityId, E] = {}

    async def get(self, entity_id: ID) -> E | None:
        if entity_id in self._identity_map:
            return self._identity_map[entity_id]

        dto = await self._session.get(self.dto_class, entity_id.value)

        if dto is not None:
            return self.map_dto_to_entity_and_track(dto)

        return None

    def add(self, entity: E) -> None:
        dto = self._map_entity_to_dto(entity)
        self._session.add(dto)
        self._track(entity)

    async def remove(self, entity: E) -> None:
        dto = self._map_entity_to_dto(entity)
        await self._session.delete(dto)
        self._identity_map.pop(entity.id, None)

    @final
    def map_dto_to_entity_and_track(self, dto: DTO) -> E:
        entity = self._map_dto_to_entity(dto)
        self._track(entity)
        return entity

    @abc.abstractmethod
    def _map_entity_to_dto(self, entity: E) -> DTO:
        """Convert a domain entity to a DTO."""
        pass

    @abc.abstractmethod
    def _map_dto_to_entity(self, dto: DTO) -> E:
        """Convert a DTO to a domain entity."""
        pass

    def _track(self, entity: E) -> None:
        self._identity_map[entity.id] = entity

    async def sync_state(self) -> None:
        for entity in self._identity_map.values():
            dto = self._map_entity_to_dto(entity)
            await self._session.merge(dto)


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
