from whetstone.domain.change import (
    AddedLine,
    FileChange,
    parse_hunk_added_lines,
    parse_unified_diff,
    replace_added_lines,
    reverse_hunks,
)
from whetstone.domain.refs import RepoRef

REPO = RepoRef.parse("gitlab:acme/payments")

DIFF = """\
diff --git a/src/handlers/charge.rs b/src/handlers/charge.rs
--- a/src/handlers/charge.rs
+++ b/src/handlers/charge.rs
@@ -40,5 +40,6 @@ impl ChargeHandler {
     pub fn charge(&self, id: u64) -> Response {
-        let row = self.db.get(id);
+        let row = self.db.get(id).unwrap();
         Response::ok(row)
     }
 }
"""


def test_parses_path_and_added_line_number() -> None:
    change = parse_unified_diff(DIFF, repo=REPO)
    assert [f.path for f in change.files] == ["src/handlers/charge.rs"]
    added = change.files[0].added
    assert len(added) == 1
    # context line at 40, removed line does not advance, added line lands at 41.
    assert added[0].line == 41
    assert ".unwrap()" in added[0].content


def test_removed_lines_do_not_advance_new_counter() -> None:
    change = parse_unified_diff(DIFF, repo=REPO)
    assert change.files[0].added_line_numbers() == [41]


def test_captures_raw_diff_faithfully() -> None:
    # The parsed change must round-trip back to a diff that still has the CONTEXT and REMOVED lines,
    # not just the added ones — the reviewer needs that context.
    change = parse_unified_diff(DIFF, repo=REPO)
    file = change.file("src/handlers/charge.rs")
    assert file is not None
    assert "-        let row = self.db.get(id);" in file.raw_diff  # removed line preserved
    assert "pub fn charge" in file.raw_diff  # context preserved

    rebuilt = parse_unified_diff(change.to_unified_diff(), repo=REPO)
    assert rebuilt.file("src/handlers/charge.rs").added_line_numbers() == [41]  # type: ignore[union-attr]
    assert "-        let row = self.db.get(id);" in change.to_unified_diff()


def test_backslash_no_newline_marker_does_not_shift_line_numbers() -> None:
    # `\ No newline at end of file` sits mid-hunk (after the removed line, before the added ones).
    # It must NOT advance the new-file counter, or the added lines shift from [1, 2] to [2, 3].
    diff = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,1 +1,2 @@
-a
\\ No newline at end of file
+a
+b
"""
    change = parse_unified_diff(diff, repo=REPO)
    assert change.file("x.py").added_line_numbers() == [1, 2]  # type: ignore[union-attr]


# --- reversal: reconstructing how a defect entered from the fix ----------------


def _file(body: str, path: str = "a.rs") -> FileChange:
    return FileChange(path=path, added=parse_hunk_added_lines(body), raw_diff=body)


FIX = (
    "@@ -40,3 +40,3 @@ impl H {\n"
    "     fn charge(&self) {\n"
    "-        let row = db.get(id).unwrap();\n"
    "+        let row = db.get(id)?;\n"
    "         ok(row)\n"
)


def test_reversal_swaps_additions_and_removals() -> None:
    reversed_body = reverse_hunks(FIX)
    assert "+        let row = db.get(id).unwrap();" in reversed_body
    assert "-        let row = db.get(id)?;" in reversed_body


def test_reversal_swaps_the_hunk_sides() -> None:
    assert reverse_hunks(FIX).startswith("@@ -40,3 +40,3 @@ impl H {")


def test_reversal_preserves_the_function_context() -> None:
    assert "impl H {" in reverse_hunks(FIX)


def test_reversal_round_trips() -> None:
    # Reversing twice is the identity: line order is untouched, only the +/- sense flips.
    assert reverse_hunks(reverse_hunks(FIX)) == FIX


def test_reversed_change_points_the_expectation_at_the_defect() -> None:
    change = parse_unified_diff(DIFF, repo=REPO).reversed()
    file = change.files[0]
    # The fix's removal comes back as an addition — the line a reviewer should have objected to.
    assert [a.content.strip() for a in file.added] == ["let row = self.db.get(id);"]


def test_reversing_swaps_the_refs() -> None:
    change = parse_unified_diff(DIFF, repo=REPO, base_ref="old", head_ref="new").reversed()
    assert (change.base_ref, change.head_ref) == ("new", "old")


def test_pure_addition_reverses_to_nothing_addable() -> None:
    # A fix that only adds a guard clause reverses to a pure deletion: no line in the new file to
    # anchor an expectation to, and callers must notice rather than mint an unmatchable case.
    added_only = "@@ -40,1 +40,2 @@\n     fn charge() {\n+        assert!(id > 0);\n"
    assert _file(added_only).reversed().added == []


def test_reversal_recomputes_hunk_counts() -> None:
    # The source header is wrong on purpose; a reversed hunk with stale counts is not a valid diff.
    body = "@@ -1,99 +1,99 @@\n ctx\n-gone\n+new\n"
    assert reverse_hunks(body).splitlines()[0] == "@@ -1,2 +1,2 @@"


def test_reversal_survives_a_rename() -> None:
    file = FileChange(path="new.rs", old_path="old.rs", added=[AddedLine(line=1, content="x")])
    back = file.reversed()
    assert (back.path, back.old_path) == ("old.rs", "new.rs")


# --- applying a suggestion: building the accepted-fix counterpart ---------------


def test_replacing_an_added_line_swaps_the_content() -> None:
    body = "@@ -40,2 +40,3 @@\n     fn charge() {\n+        db.get(id).unwrap();\n         ok()\n"
    fixed = replace_added_lines(_file(body), (41, 41), ["        db.get(id)?;"])
    assert [a.content.strip() for a in fixed.added] == ["db.get(id)?;"]
    assert "unwrap" not in fixed.raw_diff


def test_replacement_keeps_the_surrounding_context() -> None:
    body = "@@ -40,2 +40,3 @@\n     fn charge() {\n+        db.get(id).unwrap();\n         ok()\n"
    fixed = replace_added_lines(_file(body), (41, 41), ["        db.get(id)?;"])
    assert "     fn charge() {" in fixed.raw_diff
    assert "         ok()" in fixed.raw_diff


def test_a_multi_line_replacement_recomputes_the_header() -> None:
    body = "@@ -40,1 +40,2 @@\n     fn charge() {\n+        one();\n"
    fixed = replace_added_lines(_file(body), (41, 41), ["        a();", "        b();"])
    assert [a.content.strip() for a in fixed.added] == ["a();", "b();"]
    # One context + two added lines on the new side; one context on the old.
    assert fixed.raw_diff.splitlines()[0] == "@@ -40,1 +40,3 @@"


def test_removed_lines_are_left_alone() -> None:
    fixed = replace_added_lines(_file(FIX), (41, 41), ["        let row = db.get(id).ok();"])
    assert "-        let row = db.get(id).unwrap();" in fixed.raw_diff


def test_a_range_matching_no_added_line_changes_nothing() -> None:
    original = _file(FIX)
    fixed = replace_added_lines(original, (900, 901), ["x"])
    assert [a.content for a in fixed.added] == [a.content for a in original.added]


def test_multi_file_diff() -> None:
    multi = DIFF + """\
diff --git a/src/handlers/refund.rs b/src/handlers/refund.rs
--- a/src/handlers/refund.rs
+++ b/src/handlers/refund.rs
@@ -20,2 +20,3 @@ impl RefundHandler {
     pub fn refund(&self) {
+        do_it()?;
     }
"""
    change = parse_unified_diff(multi, repo=REPO)
    assert [f.path for f in change.files] == [
        "src/handlers/charge.rs",
        "src/handlers/refund.rs",
    ]
    assert change.file("src/handlers/refund.rs").added_line_numbers() == [21]  # type: ignore[union-attr]
