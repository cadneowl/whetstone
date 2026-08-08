import { useState, type ReactNode } from 'react'
import {
  useSkillClaims,
  type ClaimHistory,
  type SidecarStatus,
  type SkillDetail,
} from '@/api/client'
import { Badge, when } from '@/components/primitives'
import { SidecarGraph } from '@/components/SidecarGraph'

/**
 * The Sidecar tab: what this skill reads from beside the code, or how to make it read any.
 *
 * Both halves matter. A skill that declares a role needs the panel below — the silent failure it
 * catches is a `source_root` that does not resolve, which reads on every other screen as a skill
 * with nothing to say. A skill that declares none needs the other half, because the feature was
 * otherwise discoverable only by already knowing it existed: nothing on any screen mentioned local
 * context, so the person best placed to adopt it had no path to it.
 */
export function SidecarTab({ detail, skillId }: { detail: SkillDetail; skillId: string }) {
  if (!detail.sidecar) return <SidecarSetup detail={detail} skillId={skillId} />
  return (
    <>
      <LocalContext sidecar={detail.sidecar} skillId={skillId} />
      {/* The panel above counts the notes; this says what they are about. Kept on the same tab
          rather than behind another one, because the two answer halves of one question and the
          count is the half that cannot tell you where to write the next note. */}
      <section>
        <h2 className="text-sm font-semibold">What these notes point at</h2>
        <p className="mt-1 mb-3 max-w-3xl text-sm text-muted">
          The rule a folder excepts, the review a claim came out of, the file a section describes,
          the folder a claim says its invariant also holds in — all of it is already in the files
          and none of it was visible anywhere. Read-only: nothing here changes what a reviewer is
          given, or any hash.
        </p>
        <SidecarGraph skillId={skillId} />
      </section>
    </>
  )
}

/**
 * What a skill reads from beside the code it reviews.
 *
 * This sits with the guidance because that is what it is: the reviewer's prompt is the rules on
 * this page *plus* the `.agents/` notes for the folders a change touches, and a screen that shows
 * only the first is describing half the instrument.
 *
 * It leads with whether the source tree resolved, because the failure worth catching is silent —
 * an unresolvable `source_root` means every case reads no local context and the run looks clean.
 * Everything else here is one number that used to require running something to learn.
 */
export function LocalContext({
  sidecar,
  skillId,
}: {
  sidecar: SidecarStatus | null | undefined
  skillId: string
}) {
  const [open, setOpen] = useState(false)
  // Null for every skill that declares no role, which is most of them.
  if (!sidecar) return null
  const {
    role,
    files,
    claims,
    uncited,
    disputed,
    problems,
    install_problems: installProblems,
    source_ok: sourceOk,
    source_root: sourceRoot,
    source_declared: declared,
    scan_truncated: truncated,
    self_collected: selfCollected,
  } = sidecar

  return (
    <section className="mb-5 rounded-lg border border-line bg-surface px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 className="text-sm font-semibold">Local context</h2>
          <code className="font-mono text-xs text-accent">.agents/{role}.md</code>
          <span
            className="text-xs text-muted"
            title="The role id comes from SKILL.md frontmatter and never from the skill's folder name, so forking this skill does not mean renaming sidecars across a monorepo."
          >
            + <code className="font-mono">context.md</code>, which every role reads
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {sourceOk ? (
            <Badge
              tone={files > 0 ? 'good' : 'neutral'}
              title="Files under the source tree this role would read. Absence is normal — there is deliberately no coverage metric, because the moment one exists somebody fills every folder and the tier becomes noise."
            >
              {files} file{files === 1 ? '' : 's'} · {claims} claim{claims === 1 ? '' : 's'}
              {truncated ? '+' : ''}
            </Badge>
          ) : problems.length > 0 ? (
            // The plan knows *why* and says so below. Asserting "tree not found" here would name a
            // cause we did not check — a declaration refused for some other reason (a task skill, a
            // missing collector) leaves the tree unbound with nothing wrong with the tree at all.
            <Badge tone="bad" title={problems.join(' · ')}>
              not resolved
            </Badge>
          ) : (
            <Badge
              tone="bad"
              title="Every case would resolve to no local context and the run would look clean while reading nothing."
            >
              source tree not found
            </Badge>
          )}
          {uncited > 0 && (
            <Badge
              tone="warn"
              title="A claim with no `<!-- src: … -->` is unfalsifiable: verification has nothing to check it against and the dead-claim sweep cannot ask whether what it recorded still holds. `whetstone sidecars check` fails these."
            >
              {uncited} uncited
            </Badge>
          )}
          {disputed > 0 && (
            <Badge
              tone="bad"
              title="Something with the code in front of it said these claims no longer hold. `whetstone sidecars claims --disputed` is the queue; correction is a human's call, never automatic."
            >
              {disputed} disputed
            </Badge>
          )}
          {installProblems.length === 0 ? (
            <Badge
              tone="neutral"
              title={
                selfCollected
                  ? "The collector copy in this skill's tools/ is byte-for-byte the one Whetstone scores with — which is what makes this panel show the same files the reviewer reads."
                  : "The collector copy in this skill's tools/ is byte-for-byte the one Whetstone scores with, so a gate taken here describes what a Claude Code session does."
              }
            >
              collector current
            </Badge>
          ) : (
            // Drift only. A collector that is missing outright is refused at the plan for a
            // self-collecting skill, so it arrives in `problems` below rather than as a badge.
            <Badge tone="warn" title={installProblems.join(' · ')}>
              collector stale
            </Badge>
          )}
        </div>
      </div>

      {/* Who does the walking is the difference between two panels, not one sentence: every cap
          below is enforced by whoever collects, and `confirmations` is a question only the built-in
          reviewer's prompt asks. Saying "the harness injects this" over a skill that collects its
          own would be the same class of untruth this whole tier is built to stop. */}
      <p className="mt-2 text-sm text-muted">
        {selfCollected ? (
          <>
            Collected by this skill&apos;s own reviewer, which calls its installed{' '}
            <code className="font-mono text-xs">tools/collect_sidecars.py</code> against{' '}
            <code className="font-mono text-xs">
              {declared || sourceRoot || '(nowhere declared)'}
            </code>
            . Whetstone resolves none of it and sends none of it, so nothing here is in any hash —
            this panel reads the same files the reviewer will, and that is all it does.
          </>
        ) : (
          <>
            Read from{' '}
            <code className="font-mono text-xs">
              {declared || sourceRoot || '(nowhere declared)'}
            </code>{' '}
            per changed path — the harness walks each path&apos;s folder up to the root and injects
            what it finds. The model never decides whether to look, so a folder&apos;s notes cannot
            be skipped on file 30 of 40.
          </>
        )}
      </p>

      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-muted">
        <li
          title={
            selfCollected
              ? 'Which folders a role pulls in. Only diff-paths is implemented. Not hashed here: the reviewer collects its own context, so the declaration identifies nothing Whetstone measured.'
              : 'Which folders a role pulls in. Only diff-paths is implemented; the field is hashed so a skill that later asks for more is not silently scored as if it had not.'
          }
        >
          scope {sidecar.scope}
        </li>
        <li
          title={
            selfCollected
              ? 'What the collector is allowed to hand back. Enforced by the installed script from tools/sidecar.json — Whetstone neither applies nor hashes it here.'
              : 'What the model is asked to hold. Over it, the most general folders are dropped first and the drop is named in the prompt and hashed.'
          }
        >
          budget {sidecar.budget.toLocaleString()}
        </li>
        <li
          title={
            selfCollected
              ? 'Bounds the IO one review can cause. Enforced by the installed collector, not by this harness.'
              : 'Bounds the IO one case can cause, before any file is read.'
          }
        >
          max_files {sidecar.max_files}
        </li>
        <li title="A sidecar over this has become the central system map this design exists to break up. It is dropped rather than read, and the CI floor fails it where splitting is cheap.">
          max_file_bytes {sidecar.max_file_bytes.toLocaleString()}
        </li>
        {/* Only the built-in reviewer's prompt carries the confirmation question
            (`llm_reviewer.py`), so for a self-collecting reviewer the setting decides nothing and
            showing it "on" would promise a maintenance signal that never arrives. */}
        {selfCollected ? (
          <li
            className="text-muted/70"
            title="Consumer confirmations are asked by the built-in reviewer's prompt. This skill's reviewer writes its own, so Whetstone cannot ask on its behalf — the ledger fills from `whetstone sidecars sweep` instead."
          >
            confirmations n/a
          </li>
        ) : (
          <li
            className={sidecar.confirmations ? 'text-accent' : undefined}
            title={
              sidecar.confirmations
                ? 'Each review is also asked whether the code still agrees with the claims it was handed. Measured to cost recall on at least one model — it is part of the hashed declaration, so turning it off retracts baselines rather than quietly changing what was measured.'
                : 'Reviews are not asked to confirm the claims they read. Off by default: the extra question is free in tokens and measured expensive in attention.'
            }
          >
            confirmations {sidecar.confirmations ? 'on' : 'off'}
          </li>
        )}
      </ul>

      {/* The maintenance loop's whole output. It existed only behind `whetstone sidecars claims`,
          which means the one thing this tier produces for a human — "something with the code in
          front of it says this note is wrong" — was invisible on every screen. */}
      <details
        className="mt-2 border-t border-line pt-2"
        onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
      >
        <summary className="cursor-pointer text-sm text-muted">
          What runs have said about these claims
          {disputed > 0 && <span className="text-bad"> · {disputed} disputed</span>}
        </summary>
        <ClaimLedger skillId={skillId} enabled={open} />
      </details>

      {problems.length > 0 && (
        <ul className="mt-2 space-y-1 border-t border-line pt-2 text-sm text-bad">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
      {installProblems.length > 0 && (
        // Whetstone's own score stays correct — it uses the canonical collector — but the gate is
        // then measuring something the user's own Claude Code session is not doing.
        <ul className="mt-2 space-y-1 border-t border-line pt-2 text-sm text-warn">
          {installProblems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
    </section>
  )
}

const CODE = 'mt-2 overflow-x-auto rounded border border-line bg-canvas p-2 font-mono text-xs'

/**
 * How to make this skill read local context, for a skill that does not.
 *
 * Written as the four things you actually do, in order, with this skill's own id in them — not as
 * a description of the feature. Someone reading this has already decided they want it; what they
 * need is the two keys, in the two files, and what to run afterwards.
 *
 * It also says when *not* to. There is deliberately no coverage metric anywhere in this design,
 * because the moment one exists somebody fills every folder and the tier becomes noise — and a
 * setup page that reads as an exhortation is the same mistake in prose.
 */
function SidecarSetup({ detail, skillId }: { detail: SkillDetail; skillId: string }) {
  const evaluate = detail.steps.find((step) => step.kind === 'evaluate')
  // An agent chooses its own reads and a program collects its own context, so Whetstone cannot
  // hash what it did not resolve — declaring a role on one of these is refused at the plan rather
  // than quietly ignored. Telling someone to add the block would be telling them to break a run.
  const selfCollecting = evaluate?.mode === 'agent' || evaluate?.mode === 'program'
  const isTask = evaluate?.mode === 'task'

  return (
    <section className="max-w-3xl space-y-4">
      <div className="rounded-lg border border-line bg-surface px-4 py-3">
        <h2 className="text-sm font-semibold">This skill reads no local context</h2>
        <p className="mt-1 text-sm text-muted">
          Its reviewer sees the guidance and the diff, and nothing about the particular folder the
          change lands in. That is the right setup for rules that hold everywhere — and the wrong
          one for a codebase whose reviews turn on things no rule can state: why this retry cap is
          3, which class is a deliberate god object, which invariant a job depends on.
        </p>
      </div>

      {isTask ? (
        <p className="text-sm text-muted">
          This is a task skill — it produces work rather than findings, and sidecars are read by the
          review path. Nothing to enable here.
        </p>
      ) : selfCollecting ? (
        // Not a refusal any more, so it no longer reads as one. What stays refused is *injection*:
        // Whetstone will not hash context it did not resolve. Everything else — the files, the
        // counts, the graph — is reading, and reading was never the thing in question.
        <ol className="space-y-4">
          <Step
            n={1}
            title="Call the collector from your reviewer"
            why={`This skill's evaluate step runs as ${
              evaluate?.mode === 'agent' ? 'an agent' : 'a program'
            }, which chooses its own reads — so Whetstone must not inject a second, host-resolved set behind its back. Install the collector and call it yourself at the start of each review: it is the same file Whetstone would have run, byte for byte.`}
          >
            <pre className={CODE}>{`whetstone sidecars install --skill skills/${skillId}
# then, from the reviewer, per changed path:
python tools/collect_sidecars.py --root "$SOURCE_ROOT" <changed paths>`}</pre>
          </Step>

          <Step
            n={2}
            title="Name the role, and say you collect it yourself"
            why="In SKILL.md frontmatter. Without `self_collected: true` the declaration is refused at the plan, because a role on a self-collecting reviewer usually means someone believes injection is happening. With it, Whetstone reads the files for this page and injects nothing — the eval digest is untouched either way."
          >
            <pre className={CODE}>{`---
id: ${skillId}
sidecar:
  role: ${suggestRole(skillId)}
  self_collected: true
---`}</pre>
          </Step>

          <Step
            n={3}
            title="Say where the reviewed tree is checked out"
            why="In evaluate/step.yaml — the same env var your reviewer resolves. Whetstone needs it only to find the .agents/ files for this page; it is refused if it is unset or is not a directory, because the alternative is a page that says this skill reads no local context and never says why."
          >
            <pre className={CODE}>{`context:
  source_root: { env: ${envName(skillId)}, required: true }`}</pre>
          </Step>

          <Step
            n={4}
            title="Write the first note, beside the code"
            why="In the source repo, in the folder it describes. Every claim carries where it came from — a review comment, a ticket, an ADR — and is rejected without one, because verification needs something to check against beyond the claim's own plausibility."
          >
            <pre className={CODE}>{`# <source repo>/payments/.agents/context.md
---
status: confirmed
---

- PaymentService.record() is the only writer to payments_ledger. A write that
  goes around it skips the idempotency check.
  <!-- src: HUB-48163#r527 -->`}</pre>
          </Step>
        </ol>
      ) : (
        <ol className="space-y-4">
          <Step
            n={1}
            title="Name the role this skill reads"
            why="In SKILL.md frontmatter, never the folder name — so forking this skill does not mean renaming files across a monorepo."
          >
            <pre className={CODE}>{`---
id: ${skillId}
sidecar:
  role: ${suggestRole(skillId)}
---`}</pre>
          </Step>

          <Step
            n={2}
            title="Say where the reviewed tree is checked out"
            why="In evaluate/step.yaml. The env var's name is committed and its value never is — the path differs on every machine. `required: true` fails the plan when it is unset, instead of silently reviewing with no local context at all."
          >
            <pre className={CODE}>{`context:
  source_root: { env: ${envName(skillId)}, required: true }`}</pre>
          </Step>

          <Step
            n={3}
            title="Install the collector"
            why="Copies the retrieval script into this skill's tools/ so the skill still reads sidecars when it runs outside Whetstone — under Claude Code, say. It is the same file Whetstone scores with, byte for byte, which is what makes a gate here describe what happens there. Commit it."
          >
            <pre className={CODE}>{`whetstone sidecars install --skill skills/${skillId}`}</pre>
          </Step>

          <Step
            n={4}
            title="Write the first note, beside the code"
            why="In the source repo, in the folder it describes. Every claim carries where it came from — a review comment, a ticket, an ADR — and is rejected without one, because verification needs something to check against beyond the claim's own plausibility."
          >
            <pre className={CODE}>{`# <source repo>/payments/.agents/context.md
---
status: confirmed
---

- PaymentService.record() is the only writer to payments_ledger. A write that
  goes around it skips the idempotency check.
  <!-- src: HUB-48163#r527 -->`}</pre>
          </Step>
        </ol>
      )}

      {selfCollecting && (
        // No ablation box here on purpose: `--no-sidecars` withholds what *Whetstone* injects, and
        // that is nothing when the reviewer collects its own. Running it would measure the same
        // thing twice and label one an ablation, so the flag is refused rather than offered.
        <p className="text-sm text-muted">
          Measuring whether the notes help is yours to arrange — withhold them inside the reviewer
          and score both ways. <code className="font-mono text-xs">--no-sidecars</code> is refused
          for this skill: it would withhold nothing and record a run indistinguishable from a normal
          one.
        </p>
      )}

      {!isTask && !selfCollecting && (
        <>
          <div className="rounded-lg border border-line bg-surface px-4 py-3 text-sm">
            <h3 className="font-semibold">Then find out whether it is worth it</h3>
            <p className="mt-1 text-muted">
              The realistic failure is not a wrong note. It is fifteen files of mediocre context on
              every run, attention diluted, findings quietly worse — invisible, because there is no
              baseline without them. Score the corpus both ways and compare:
            </p>
            <pre className={CODE}>{`whetstone eval run --skill skills/${skillId}
whetstone eval run --skill skills/${skillId} --no-sidecars`}</pre>
            <p className="mt-2 text-muted">
              The ablation records as a different measurement, so it can never reuse the
              other&apos;s baseline or be read as a regression in a trend. If recall does not move,
              this tier is costing tokens and attention for nothing.
            </p>
          </div>

          <p className="text-sm text-muted">
            Folders with no notes are normal and there is deliberately no coverage number anywhere
            in this — fill folders where reviews keep going wrong, not to reach a percentage. The
            full design is in <code className="font-mono text-xs">docs/design/sidecars.md</code>,
            and <code className="font-mono text-xs">examples/sidecar-review/</code> is a working
            fixture.
          </p>
        </>
      )}
    </section>
  )
}

function Step({
  n,
  title,
  why,
  children,
}: {
  n: number
  title: string
  why: string
  children: ReactNode
}) {
  return (
    <li className="rounded-lg border border-line bg-surface px-4 py-3">
      <p className="text-sm font-semibold">
        <span className="mr-2 text-muted tabular">{n}</span>
        {title}
      </p>
      <p className="mt-1 text-sm text-muted">{why}</p>
      {children}
    </li>
  )
}

/** A plausible role id from the skill id — a starting point to edit, not a rule. */
function suggestRole(skillId: string): string {
  return skillId.replace(/^(code-review|review)-/, '').replace(/-review$/, '') || 'review'
}

/** The env var name the source_root would read, in this repo's SHOUTY_SNAKE convention. */
function envName(skillId: string): string {
  return `${skillId.replace(/[^a-zA-Z0-9]+/g, '_').toUpperCase()}_SOURCE`
}

/**
 * One row per claim, disputed first.
 *
 * The counts are the point, not the latest verdict: one `contradicted` from one model on one case
 * is an opinion, and the same verdict from four unrelated runs over a month is a finding. A table
 * showing current status would render both identically.
 *
 * Read-only, deliberately. Confirmation is automatic and correction is not — the fix is a human
 * editing the sidecar in the repository that owns it, and no button here could do that.
 */
function ClaimLedger({ skillId, enabled }: { skillId: string; enabled: boolean }) {
  const { data, isLoading } = useSkillClaims(skillId, enabled)
  if (isLoading) return <p className="mt-2 text-sm text-muted">Reading the ledger…</p>
  if (!data || data.length === 0) {
    return (
      <p className="mt-2 text-sm text-muted">
        Nothing recorded yet. Verdicts arrive from runs whose cases touch these folders — with{' '}
        <code className="font-mono text-xs">confirmations</code> on — and from{' '}
        <code className="font-mono text-xs">whetstone sidecars verify</code>, which checks each
        folder blind and files what it finds.
      </p>
    )
  }
  return (
    <ul className="mt-2 space-y-2">
      {data.map((claim) => (
        <ClaimRow key={`${claim.path}:${claim.claim}`} claim={claim} />
      ))}
    </ul>
  )
}

function ClaimRow({ claim }: { claim: ClaimHistory }) {
  return (
    <li
      className={`rounded border px-2.5 py-1.5 text-sm ${
        claim.disputed ? 'border-bad/40 bg-bad/5' : 'border-line/60'
      }`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <code className="font-mono text-xs text-muted">{claim.path}</code>
        <span className="flex items-center gap-2 text-xs">
          {claim.contradicted > 0 && (
            <span className="text-bad" title="Runs that found code disagreeing with this claim.">
              {claim.contradicted} contradicted
            </span>
          )}
          {claim.confirmed > 0 && (
            <span className="text-good" title="Runs that cited code showing it still holds.">
              {claim.confirmed} confirmed
            </span>
          )}
          {claim.unverifiable > 0 && (
            <span
              className="text-muted"
              title="Assent with no code citation, which is recorded as what it is rather than as evidence."
            >
              {claim.unverifiable} unverifiable
            </span>
          )}
          <span className="text-muted" title="When anything last said anything about this claim.">
            {when(claim.last_seen)}
          </span>
        </span>
      </div>
      <p className="mt-1">{claim.claim}</p>
      {claim.last_evidence && (
        // Only ever evidence *against*: it is the one text a human needs to decide, and the
        // decision — edit the claim, or the code — is theirs.
        <p className="mt-1 text-sm text-bad italic">— {claim.last_evidence}</p>
      )}
    </li>
  )
}
