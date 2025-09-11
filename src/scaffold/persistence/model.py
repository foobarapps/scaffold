import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import (
    DeclarativeBaseNoMeta,
    Mapped,
    mapped_column,
)


class EntityMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(),
        primary_key=True,
        sort_order=-1,
    )


class TimestampMixin:
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime,
        nullable=False,
        server_default=sa.func.timezone("UTC", sa.func.now()),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime,
        nullable=False,
        server_default=sa.func.timezone("UTC", sa.func.now()),
        onupdate=datetime.datetime.now(datetime.UTC),
    )


class Base(DeclarativeBaseNoMeta):
    pass
