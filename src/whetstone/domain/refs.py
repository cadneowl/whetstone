from __future__ import annotations

from pydantic import BaseModel


class RepoRef(BaseModel, frozen=True):
    """Provider-neutral repository reference, e.g. ``gitlab:acme/payments``."""

    provider: str
    path: str

    @property
    def slug(self) -> str:
        return f"{self.provider}:{self.path}"

    @classmethod
    def parse(cls, slug: str) -> RepoRef:
        provider, _, path = slug.partition(":")
        if not path:
            raise ValueError(f"repo ref must be '<provider>:<path>', got {slug!r}")
        return cls(provider=provider, path=path)


class Region(BaseModel):
    """A location in the change: a file path and an optional inclusive line range."""

    path: str
    line_range: tuple[int, int] | None = None

    def contains(self, path: str, line: int | None) -> bool:
        if path != self.path:
            return False
        if self.line_range is None:
            return True
        if line is None:
            return False
        lo, hi = self.line_range
        return lo <= line <= hi
