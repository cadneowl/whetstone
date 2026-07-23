from __future__ import annotations

from enum import IntEnum


class Severity(IntEnum):
    """Ordered so `>=` comparisons express a minimum-severity threshold."""

    info = 10
    warning = 20
    error = 30

    @classmethod
    def parse(cls, value: str | int | Severity) -> Severity:
        if isinstance(value, Severity):
            return value
        if isinstance(value, int):
            return cls(value)
        return cls[value.strip().lower()]
