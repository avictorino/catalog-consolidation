from __future__ import annotations

from consolidation.schema import new_uuid


def test_new_uuid_shape() -> None:
    value = new_uuid()
    assert len(value) == 36
    assert value.count("-") == 4
