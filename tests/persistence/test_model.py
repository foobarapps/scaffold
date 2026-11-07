import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, Session, mapped_column

from scaffold.persistence.model import Base, EntityMixin, TimestampMixin


class Note(EntityMixin, TimestampMixin, Base):
    __tablename__ = "note"

    text: Mapped[str] = mapped_column(sa.Text)


@pytest.fixture
def engine(postgres_dsn: str, schema_name: str) -> Iterator[sa.Engine]:
    engine = sa.create_engine(
        sa.make_url(postgres_dsn).set(drivername="postgresql+psycopg"),
        execution_options={"schema_translate_map": {None: schema_name}},
    )
    with engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema_name}"'))
        Base.metadata.create_all(connection, tables=[Base.metadata.tables[Note.__tablename__]])
    yield engine
    engine.dispose()


def test_updated_at_is_when_the_row_was_last_updated(engine: sa.Engine) -> None:
    with Session(engine) as session:
        note = Note(id=uuid.uuid4(), text="first")
        session.add(note)
        session.commit()
        created_at = note.created_at

        note.text = "second"
        session.commit()
        first_update = note.updated_at

        note.text = "third"
        session.commit()

        assert created_at < first_update < note.updated_at
