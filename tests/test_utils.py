from scaffold.utils import find_subclasses
from tests.fixtures import subclasses_pkg
from tests.fixtures.subclasses_pkg import Base
from tests.fixtures.subclasses_pkg.first import First
from tests.fixtures.subclasses_pkg.nested.second import Second


def test_find_subclasses_returns_subclasses_from_nested_modules_once() -> None:
    subclasses = find_subclasses(subclasses_pkg, Base)

    assert sorted(subclasses, key=lambda cls: cls.__name__) == [First, Second]


def test_find_subclasses_returns_classes_typed_as_the_base_class() -> None:
    subclasses: list[type[Base]] = find_subclasses(subclasses_pkg, Base)

    assert all(issubclass(cls, Base) for cls in subclasses)
