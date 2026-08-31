"""Interchangeable similarity backends for identity resolution."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Protocol


class Similarity(Protocol):
    name: str
    suggested_threshold: float

    def score(self, a: str, b: str) -> float:
        """Return a score in the inclusive range [0, 1]."""


class DifflibSimilarity:
    name = "difflib"
    suggested_threshold = 0.90

    def score(self, a: str, b: str) -> float:
        return SequenceMatcher(None, a, b, autojunk=False).ratio()


class RapidFuzzSimilarity:
    name = "rapidfuzz"
    suggested_threshold = 0.90

    def score(self, a: str, b: str) -> float:
        from rapidfuzz import fuzz

        return fuzz.ratio(a, b) / 100.0


def build_similarity(name: str) -> Similarity:
    """Build a backend; rapidfuzz is imported only when this path is used."""
    if name == "difflib":
        return DifflibSimilarity()
    if name == "rapidfuzz":
        return RapidFuzzSimilarity()
    raise ValueError(f"unknown matcher: {name!r} (options: difflib, rapidfuzz)")
