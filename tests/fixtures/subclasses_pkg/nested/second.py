from tests.fixtures.subclasses_pkg import Base
from tests.fixtures.subclasses_pkg.first import First


class Second(Base):
    pass


# Re-exported here, so it must not be reported twice.
__all__ = ["First", "Second"]
