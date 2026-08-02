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

    def admits(self, path: str, line: int | None) -> bool:
        """Whether a finding at `path`:`line` may stand for something asserted about this region.

        A finding that names no line at all is admitted rather than discarded. It cannot be placed,
        so the only honest thing to do is let the judge read it: a custom reviewer that reports the
        right problem without a line number would otherwise fail every case it got right, silently
        and on a technicality, with the drill-down reporting it as out of range when in truth it was
        never in a position to be in range.
        """
        if path != self.path:
            return False
        if self.line_range is None or line is None:
            return True
        lo, hi = self.line_range
        return lo <= line <= hi
