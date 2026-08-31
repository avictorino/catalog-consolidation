from __future__ import annotations

import pytest

from consolidation.schema import new_uuid, normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Smartphone  Galaxy S23", "smartphone galaxy s23"),
        ("Câmera Canon EOS R6", "camera canon eos r6"),
        ("Camera Canon EOS R6", "camera canon eos r6"),
        ('iPad Pro 12.9"', "ipad pro 12 9"),
        ("iPad Pro 12.9''", "ipad pro 12 9"),
        ("iPad Pro 12.9", "ipad pro 12 9"),
        ("BLACK+DECKER", "black decker"),
        ("Black+Decker", "black decker"),
        ("Simplehuman", "simplehuman"),
        ("simplehuman", "simplehuman"),
        ("Levi's", "levis"),
        ("Levis", "levis"),
        ("Levi’s", "levis"),  # noqa: RUF001 -- curly apostrophe is the point
        ("  trailing and leading  ", "trailing and leading"),
        (None, ""),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_normalize(raw: str | None, expected: str) -> None:
    assert normalize(raw) == expected


def test_normalize_is_idempotent() -> None:
    once = normalize('Câmera  Canon EOS R6 12.9"')
    assert normalize(once) == once


def test_new_uuid_shape() -> None:
    value = new_uuid()
    assert len(value) == 36
    assert value.count("-") == 4
