import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * Without this, a render error anywhere unmounts the whole tree and leaves a blank white page —
 * indistinguishable from the server being down. A crash should still say what broke and let you
 * get back to a working screen.
 */
export class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // The console is a local dev tool; the browser console is where its owner will look.
    console.error('Console render error:', error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="mx-auto max-w-2xl px-5 py-12">
        <h1 className="mb-2 text-lg font-semibold text-bad">Something broke rendering this page</h1>
        <p className="mb-4 text-sm text-muted">
          The API is probably fine — this is a bug in the console. The details are in the browser
          console.
        </p>
        <pre className="mb-4 overflow-x-auto rounded-lg border border-line bg-surface p-3 font-mono text-xs">
          {error.message}
        </pre>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            className="rounded-lg border border-line px-3 py-1.5 text-sm transition-colors hover:border-accent/50"
          >
            Try again
          </button>
          <a
            href="/"
            className="rounded-lg border border-line px-3 py-1.5 text-sm transition-colors hover:border-accent/50"
          >
            Back to skills
          </a>
        </div>
      </div>
    )
  }
}
