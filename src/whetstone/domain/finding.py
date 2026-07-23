from __future__ import annotations

from pydantic import BaseModel

from whetstone.domain.enums import Severity


class Finding(BaseModel):
    """A single issue a reviewer raised while running a skill over a change."""

    skill_id: str
    rule_id: str | None = None
    path: str
    line: int | None = None
    severity: Severity = Severity.warning
    message: str = ""
