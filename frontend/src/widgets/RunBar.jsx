import { useEffect, useState } from 'react'

const clock = (secs) => `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`

// shown only while a batch run is sitting in a dwell, so a normal run keeps the
// old canvas without an extra bar
export const RunBar = ({ run, onStop }) => {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!run?.until) return

    const id = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(id)
  }, [run?.until])

  if (!run) return null

  // clamping to the announced length hides a stale clock on the first render
  const total = run.seconds ?? Number.MAX_SAFE_INTEGER
  const left = Math.max(0, Math.min(total, Math.round((new Date(run.until) - now) / 1000)))

  return (
    <div className="flex items-center gap-3 border-b border-subtle bg-card px-4 py-2 text-[11px]">
      <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-warning" />
      <span className="shrink-0 text-secondary">{run.phase}</span>
      <span className="shrink-0 font-mono tabular-nums text-warning">{clock(left)}</span>
      <span className="min-w-0 truncate text-muted" title={run.reason}>
        {run.reason || 'dwell before the next step'}
      </span>
      <button
        type="button"
        onClick={onStop}
        className="ml-auto shrink-0 rounded-md border border-subtle px-2 py-0.5 text-secondary transition-colors hover:border-danger hover:text-danger"
      >
        Stop
      </button>
    </div>
  )
}
