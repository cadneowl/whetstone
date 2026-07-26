You are tightening the review guidance for `{{skill_id}}`.

Its current recall is {{recall}} and its false-positive rate is {{fp_rate}}, measured over
{{cases_scored}} of {{cases_total}} eval cases. Those cases are real code review outcomes: a human
either flagged this code, or deliberately did not.

The reviewer got {{failure_count}} things wrong. Below are {{shown_count}} of them, one per kind of
failure, largest group first.

{{failures}}

## Current guidance

{{guidance}}

## Repo context

{{wiki}}

## What to do

Rewrite the guidance so those failures would not recur.

{{instruction}}

- Keep every rule that is already working. You are seeing a sample of failures, not the whole
  picture, and a rule you have no evidence about is still load-bearing.
- Prefer sharpening an existing rule over adding a new one. Guidance that grows a rule per failure
  becomes a checklist no model can apply consistently.
- A false positive usually means a rule needs a stated exception, not deletion.
- A miss usually means a rule is too abstract to recognise the pattern in a diff. Say what the code
  looks like.
- Write rules that a reader can apply to a diff without access to the rest of the repository.

Return the complete new guidance body, the rationale for the change, and the ids of the eval cases
this change is meant to fix.

`{{instruction}}` above is whatever was passed to `--instruction` on this run, and is empty on a
plain run. Move it wherever you want it read; delete it and a passed instruction is appended at the
end instead, so it is never silently dropped.
