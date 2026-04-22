import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import (
    DeclarativeBaseNoMeta,
    Mapped,
    MappedAsDataclass,
    mapped_column,
)


class EntityMixin(MappedAsDataclass):
    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(),
        primary_key=True,
        init=True,
        sort_order=-1,
    )


class TimestampMixin(MappedAsDataclass):
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime,
        init=False,
        nullable=False,
        server_default=sa.func.timezone("UTC", sa.func.now()),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime,
        init=False,
        nullable=False,
        server_default=sa.func.timezone("UTC", sa.func.now()),
        onupdate=datetime.datetime.now(datetime.UTC),
    )


class Base(MappedAsDataclass, DeclarativeBaseNoMeta):
    pass
