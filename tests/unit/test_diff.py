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
