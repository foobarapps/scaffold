import abc
import typing
from collections.abc import Hashable

from .uow import BaseSqlUnitOfWork


class Mapper[E, DTO](typing.Protocol):
    def map_persistence_to_domain(self, dto: DTO, /) -> E: ...

    def map_domain_to_persistence(self, entity: E, /) -> DTO: ...


class GenericSqlRepository[E, DTO](abc.ABC):
    mapper: Mapper[E, DTO]

    def __init__(self, uow: BaseSqlUnitOfWork) -> None:
        self._session = uow.session
        self._identity_map: dict[Hashable, tuple[E, DTO]] = {}
        self._new_entities: dict[Hashable, E] = {}
        self._deleted_entities: dict[Hashable, E] = {}
        uow.register_repository(self)

    @typing.final
    def add(self, entity: E, /) -> None:
        if self._key(entity) in self._identity_map:
            msg = "Cannot add an entity that is already tracked as persisted"
            raise ValueError(msg)

        self._new_entities[self._key(entity)] = entity

    @typing.final
    async def remove(self, entity: E, /) -> None:
        key = self._key(entity)

        if key in self._new_entities:
            del self._new_entities[key]
            return

        if key in self._deleted_entities:
            return

        if key not in self._identity_map:
            msg = "Entity must be tracked by the Unit of Work before removal"
            raise ValueError(msg)

        self._deleted_entities[key] = entity

    @typing.final
    def _track(self, entity: E, persistence_obj: DTO) -> E:
        self._identity_map[self._key(entity)] = (entity, persistence_obj)
        return entity

    @typing.final
    async def flush(self) -> None:
        new_keys = set(self._new_entities)
        new_entities = list(self._new_entities.values())

        for entity in new_entities:
            persistence_obj = self.mapper.map_domain_to_persistence(entity)
            self._session.add(persistence_obj)
            # Tracked as persisted from here on, so that a second flush updates the row
            # instead of inserting it again.
            self._track(entity, persistence_obj)
        self._new_entities.clear()

        for key, (entity, _) in self._identity_map.items():
            if key in new_keys or key in self._deleted_entities:
                continue

            await self._session.merge(self.mapper.map_domain_to_persistence(entity))

        for key in self._deleted_entities:
            tracked = self._identity_map.get(key)

            if tracked is None:
                msg = "Entity must exist in the database before removal"
                raise ValueError(msg)

            await self._session.delete(tracked[1])

        await self._after_flush(new_entities)

    async def _after_flush(self, new_entities: list[E]) -> None:  # noqa: B027
        """
        Called at the end of a flush with the entities it added. Does nothing by default.

        Override it to copy back what the database generated for a new row, e.g. a serial
        number the entity carries next to its id: flush the session, then read the value.
        """

    @typing.final
    def clear_tracking(self) -> None:
        self._identity_map.clear()
        self._new_entities.clear()
        self._deleted_entities.clear()

    @typing.final
    def _map_persistence_to_domain(self, dto: DTO) -> E:
        return self._track(self.mapper.map_persistence_to_domain(dto), dto)

    @staticmethod
    def _key(entity: E) -> Hashable:
        # An entity that defines equality is tracked by it, so two instances of the same
        # entity are one entry. One that cannot be hashed is tracked as the object it is.
        return entity if isinstance(entity, Hashable) else id(entity)
