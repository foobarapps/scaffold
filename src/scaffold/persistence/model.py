import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    MappedAsDataclass,
    mapped_column,
)


class TimestampMixin(MappedAsDataclass):
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime,
        nullable=False,
        server_default=sa.func.timezone("UTC", sa.func.now()),
        init=False,
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime,
        nullable=False,
        server_default=sa.func.timezone("UTC", sa.func.now()),
        onupdate=datetime.datetime.utcnow,
        init=False,
    )


class EntityMixin(MappedAsDataclass):
    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        sort_order=-1,
    )


class Base(MappedAsDataclass, DeclarativeBase, kw_only=True):
    pass
