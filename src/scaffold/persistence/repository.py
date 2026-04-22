import abc
import typing

from .uow import BaseSqlUnitOfWork


class Mapper[E, DTO](typing.Protocol):
    def map_persistence_to_domain(self, dto: DTO) -> E: ...

    def map_domain_to_persistence(self, entity: E) -> DTO: ...


class GenericSqlRepository[E, DTO](abc.ABC):
    mapper: Mapper[E, DTO]

    def __init__(self, uow: BaseSqlUnitOfWork) -> None:
        self._session = uow.session
        self._identity_map: dict[E, DTO] = {}
        self._new_entities: set[E] = set()
        self._deleted_entities: set[E] = set()
        uow.register_repository(self)

    @typing.final
    def add(self, entity: E, /) -> None:
        if entity in self._identity_map:
            msg = "Cannot add an entity that is already tracked as persisted"
            raise ValueError(msg)

        self._new_entities.add(entity)

    @typing.final
    async def remove(self, entity: E, /) -> None:
        if entity in self._new_entities:
            self._new_entities.discard(entity)
            return

        if entity in self._deleted_entities:
            return

        if entity not in self._identity_map:
            msg = "Entity must be tracked by the Unit of Work before removal"
            raise ValueError(msg)

        self._deleted_entities.add(entity)

    @typing.final
    def _track(self, entity: E, persistence_obj: DTO) -> E:
        self._identity_map[entity] = persistence_obj
        return entity

    @typing.final
    async def flush(self) -> None:
        for entity in self._new_entities:
            self._session.add(self.mapper.map_domain_to_persistence(entity))

        for entity in self._identity_map:
            if entity in self._deleted_entities:
                continue

            await self._session.merge(self.mapper.map_domain_to_persistence(entity))

        for entity in self._deleted_entities:
            persistence_obj = self._identity_map.get(entity)

            if persistence_obj is None:
                msg = "Entity must exist in the database before removal"
                raise ValueError(msg)

            await self._session.delete(persistence_obj)

    @typing.final
    def clear_tracking(self) -> None:
        self._identity_map.clear()
        self._new_entities.clear()
        self._deleted_entities.clear()

    @typing.final
    def _map_persistence_to_domain(self, dto: DTO) -> E:
        return self._track(self.mapper.map_persistence_to_domain(dto), dto)
