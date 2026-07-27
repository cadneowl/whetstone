import { useEffect, useState } from 'react'
import { ApiError, useModelChoice, useSetModel } from '@/api/client'

/**
 * The one control that changes which model everything runs against — reviews, evals, gates, and
 * the improve and triage drafters.
 *
 * It shows what a run launched right now would resolve to, and opens a small form to change it. The
 * choice is a provider (one Whetstone knows, whose host is fixed) plus an optional model id; a base
 * URL is deliberately not offered, so the browser can never redirect model traffic to an arbitrary
 * host. Read-only consoles get the label without the form, matching every other write control.
 */
export function ModelPicker({ readOnly }: { readOnly: boolean }) {
  const { data } = useModelChoice()
  const [open, setOpen] = useState(false)

  if (!data) return null

  const effective = data.resolved_model || data.resolved_backend || 'default'
  const label = data.note ? 'model: unresolved' : `model: ${effective}`
  const title = data.note
    ? data.note
    : `${data.resolved_label || data.resolved_backend} · ${data.resolved_model}` +
      (data.base_url ? ` · ${data.base_url}` : '')

  if (readOnly) {
    return (
      <span className="font-mono" title={title}>
        {label}
      </span>
    )
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title={title}
        className="rounded border border-line px-2 py-px font-mono text-xs text-muted transition-colors hover:text-ink"
      >
        {label}
      </button>
      {open && <ModelForm choice={data} onClose={() => setOpen(false)} />}
    </div>
  )
}

function ModelForm({
  choice,
  onClose,
}: {
  choice: NonNullable<ReturnType<typeof useModelChoice>['data']>
  onClose: () => void
}) {
  const setModel = useSetModel()
  const [provider, setProvider] = useState(choice.provider)
  const [model, setModel_] = useState(choice.model)

  // Re-seed the fields whenever the panel reopens against a freshly-fetched choice, so it never
  // shows a stale value from a previous open.
  useEffect(() => {
    setProvider(choice.provider)
    setModel_(choice.model)
  }, [choice.provider, choice.model])

  const error = setModel.error
  const message = error instanceof ApiError ? error.message : error ? String(error) : ''

  function save() {
    setModel.mutate(
      { provider: provider.trim(), model: model.trim() },
      { onSuccess: onClose },
    )
  }

  return (
    <>
      {/* A click anywhere else dismisses without saving. */}
      <div className="fixed inset-0 z-10" onClick={onClose} />
      <div className="absolute right-0 z-20 mt-2 w-80 rounded-lg border border-line bg-surface p-3 text-left shadow-lg">
        <p className="mb-2 text-xs text-muted">
          The model used for reviews, runs, gates and drafting. The launch plan still shows the
          exact backend before anything spends.
        </p>

        <label className="block text-xs text-muted">
          Provider
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            className="mt-1 block w-full rounded border border-line bg-canvas px-2 py-1.5 text-sm text-ink"
          >
            <option value="">Configured default</option>
            {choice.available.map((b) => (
              <option key={b.name} value={b.name}>
                {b.label}
              </option>
            ))}
          </select>
        </label>

        <label className="mt-2 block text-xs text-muted">
          Model
          <input
            value={model}
            onChange={(e) => setModel_(e.target.value)}
            placeholder={choice.resolved_model || 'provider default'}
            spellCheck={false}
            className="mt-1 block w-full rounded border border-line bg-canvas px-2 py-1.5 font-mono text-xs text-ink"
          />
          <span className="mt-1 block">
            Leave blank for the provider's default. A local or cloud provider needs a model id
            here.
          </span>
        </label>

        {message && <p className="mt-2 text-xs text-bad">{message}</p>}

        <div className="mt-3 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded px-2 py-1 text-xs text-muted transition-colors hover:text-ink"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={save}
            disabled={setModel.isPending}
            className="rounded border border-accent/40 px-3 py-1 text-xs text-accent transition-colors hover:bg-accent/10 disabled:opacity-50"
          >
            {setModel.isPending ? 'Saving…' : 'Use this model'}
          </button>
        </div>
      </div>
    </>
  )
}
