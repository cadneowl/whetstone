The guidance you are about to rewrite is your own — it is in your instructions above.

This skill is graded by writing tests and then planting bugs in the code those tests cover. A
mutant that **survives** is not a score, it is a sentence: *your tests permit this specific wrong
behaviour*. Your own `references/mutation-testing.md` says to read survivors that way and to fix
each one with a specific assertion. Do that to the guidance itself.

{{failures}}

{{instruction}}

## How to think about the change

**Fix the rule where the rule lives.** `SKILL.md` is a router — a table that picks a test type, a
seven-item quality bar, a refusal list. The how-to is in the pages:

{{pages}}

Read the page a survivor implicates before you write anything, with `read_skill_file` and the exact
path above. If the gap is in how to write a boundary table, it belongs in `references/unit-testing.md`
and not restated in `SKILL.md` — return that page's complete new text in `pages` under the path you
read it from, and leave `body` alone. Return only pages you actually changed.

**Prefer sharpening a rule to adding one.** Every survivor is tempting to answer with a new bullet,
and a skill that gains a bullet per cycle is how guidance bloats until nobody reads to the end of
it. Ask first whether an existing rule was right but too vague to act on — "cover the ugly paths"
and "cover the ugly paths: for every comparison, write the case at exactly the boundary as well as
either side of it" are the same rule, and only one of them changes what gets written.

**Keep every rule you cannot see evidence against.** The cases you are shown are a sample. A rule
with no failure behind it is load-bearing, not unused, and this guidance is adopted from outside —
most of it has never been measured at all, which is not the same as being wrong.

**Stay honest about scope.** The corpus this skill is scored on is Python, and the guidance's worked
examples are mostly Java. Do not delete the Java patterns to make the failures go away: the routing
table, the quality bar and the refusal list are the language-independent part, and they are what a
failure here is usually about. If a rule genuinely only makes sense in one ecosystem, say which.

Return the complete new guidance with `submit_guidance`, and name in `targeted_cases` the cases the
change is meant to fix.
