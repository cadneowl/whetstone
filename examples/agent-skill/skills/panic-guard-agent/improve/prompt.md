The guidance you are about to rewrite is your own — it is in your instructions above. Here is how it
did on a corpus of real, human-labelled cases.

{{failures}}

Rewrite it so those failures would not recur.

**Check the code before you write a rule about it.** Every failure below names a file and a
function; `grep` the source for the callee and read its docstring. A rule written from the diff
alone is how this skill acquired the false positives it has: `safe_get` and `balance_of` look
risky and are not, and only a reviewer that went and looked can tell.

Keep every rule you cannot see evidence against — the cases here are a sample, and a rule with no
failure in it is load-bearing, not unused.

When a rule is about one module in particular, call `owner_of` and name the team in the rule, so
whoever hits it knows who to ask.

Return the complete new guidance with `submit_guidance`. If the rule that needs changing lives in
`references/panics.md`, read that page first and return its full new text under `pages`.

{{instruction}}
