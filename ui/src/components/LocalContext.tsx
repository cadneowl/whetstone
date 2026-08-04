import { useState } from 'react'
import { useSkillClaims, type ClaimHistory, type SidecarStatus } from '@/api/client'
import { Badge, when } from '@/components/primitives'

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
              title="The collector copy in this skill's tools/ is byte-for-byte the one Whetstone scores with, so a gate taken here describes what a Claude Code session does."
            >
              collector current
            </Badge>
          ) : (
            <Badge tone="warn" title={installProblems.join(' · ')}>
              collector stale
            </Badge>
          )}
        </div>
      </div>

      <p className="mt-2 text-sm text-muted">
        Read from{' '}
        <code className="font-mono text-xs">{declared || sourceRoot || '(nowhere declared)'}</code>{' '}
        per changed path — the harness walks each path's folder up to the root and injects what it
        finds. The model never decides whether to look, so a folder's notes cannot be skipped on
        file 30 of 40.
      </p>

      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-muted">
        <li title="Which folders a role pulls in. Only diff-paths is implemented; the field is hashed so a skill that later asks for more is not silently scored as if it had not.">
          scope {sidecar.scope}
        </li>
        <li title="What the model is asked to hold. Over it, the most general folders are dropped first and the drop is named in the prompt and hashed.">
          budget {sidecar.budget.toLocaleString()}
        </li>
        <li title="Bounds the IO one case can cause, before any file is read.">
          max_files {sidecar.max_files}
        </li>
        <li title="A sidecar over this has become the central system map this design exists to break up. It is dropped rather than read, and the CI floor fails it where splitting is cheap.">
          max_file_bytes {sidecar.max_file_bytes.toLocaleString()}
        </li>
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
