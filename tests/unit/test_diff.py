from whetstone.domain.change import parse_unified_diff
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
