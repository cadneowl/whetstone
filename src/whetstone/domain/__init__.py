"""Canonical, provider-agnostic domain model.

Nothing in this package may import a provider. Connectors normalize *into* these types;
the core loop only ever sees these.
"""

from whetstone.domain.change import AddedLine, CodeChange, FileChange, parse_unified_diff
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase, EvalKind, Expectation, Must, Provenance
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.review import (
    FileBlob,
    MergeRequestRef,
    ReviewComment,
    ReviewedChange,
    ReviewThread,
    Suggestion,
)
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.domain.skill import Reference, Skill, Triggers

__all__ = [
    "AddedLine",
    "CaseScore",
    "CodeChange",
    "Confusion",
    "EvalCase",
    "EvalKind",
    "Expectation",
    "FileBlob",
    "FileChange",
    "Finding",
    "MergeRequestRef",
    "Must",
    "Provenance",
    "Reference",
    "Region",
    "RepoRef",
    "ReviewComment",
    "ReviewThread",
    "ReviewedChange",
    "Severity",
    "Skill",
    "SkillScore",
    "Suggestion",
    "Triggers",
    "parse_unified_diff",
]
