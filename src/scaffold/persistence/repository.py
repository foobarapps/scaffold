import abc
import typing

from .uow import BaseSqlUnitOfWork


class Mapper[E, DTO](typing.Protocol):
    def map_persistence_to_domain(self, dto: DTO) -> E: ...

    # TODO rename to "map" or "apply_changes"?
    def sync_domain_to_persistence(self, entity: E, dto: DTO | None = None) -> DTO: ...


class GenericSqlRepository[E, DTO](abc.ABC):
    mapper: Mapper[E, DTO]

    def __init__(self, uow: BaseSqlUnitOfWork) -> None:
        self._session = uow.session
        # Identity map tracks domain objects to their persistence counterparts.
        self._identity_map: dict[E, DTO] = {}
        # Automatically register this repository with the UoW.
        uow.register_repository(self)

    @typing.final
    def add(self, entity: E, /) -> None:
        persistence_obj = self.mapper.sync_domain_to_persistence(entity)

        self._identity_map[entity] = persistence_obj

        self._session.add(persistence_obj)

    @typing.final
    async def remove(self, entity: E, /) -> None:
        try:
            persistence_obj = self._identity_map.pop(entity)
        except KeyError:
            msg = "Entity must be tracked by the Unit of Work before removal"
            raise ValueError(msg) from None

        await self._session.delete(persistence_obj)

    @typing.final
    def _map_persistence_to_domain_and_track(self, dto: DTO) -> E:
        entity = self.mapper.map_persistence_to_domain(dto)
        self._identity_map[entity] = dto
        return entity

    @typing.final
    def sync(self) -> None:
        for domain_obj, persistence_obj in self._identity_map.items():
            self.mapper.sync_domain_to_persistence(domain_obj, persistence_obj)

    @typing.final
    def clear_identity_map(self) -> None:
        """Clear the identity map after a successful commit."""
        self._identity_map.clear()
