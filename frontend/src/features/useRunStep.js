import { useState, useCallback } from 'react'
import { stepApi } from '../entities/stepApi'
import { chainApi } from '../entities/chainApi'
import { STEP_STATUS } from '../entities/types'

// task death as the backend reports it: mythic sends error/cancelled,
// bash sends fail/failed
const FAILED_STATUSES = new Set(['fail', 'failed', 'error', 'cancelled'])

const cleanBytesOutput = (output) => {
  // output is raw cuz different langs on remote hosts
  if (!output || typeof output !== 'string') return output
  if (output.startsWith('b"') && output.endsWith('"')) {
    return unescapePythonBytes(output.slice(2, -1))
  }
  if (output.startsWith("b'") && output.endsWith("'")) {
    return unescapePythonBytes(output.slice(2, -1))
  }
  return output
}

const unescapePythonBytes = (s) =>
  // clean b'' strings of raw output
  s.replace(/\\(.)/g, (_, char) => {
    switch (char) {
      case 'n':
        return '\n'
      case 't':
        return '\t'
      case 'r':
        return '\r'
      case '\\':
        return '\\'
      default:
        return `\\${char}`
    }
  })

export const useRunStep = () => {
  const [runningIds, setRunningIds] = useState(new Set())

  const run = useCallback(async (step, phaseName, chainId, onUpdate) => {
    const id = step.id
    if (id == null) return

    if (chainId && phaseName) {
      // cuz backend doesn't know current phase
      await chainApi.setPhase(chainId, phaseName)
    }

    // we cannot run steps in parallel for now, works for most kill chains
    setRunningIds((prev) => new Set(prev).add(id))
    onUpdate?.(step, { status: STEP_STATUS.RUNNING, raw_log: '' })

    try {
      // the command is read from the db, not from the editor state
      const data = await stepApi.runStoredStep(id)
      // the backend promotes the draft when it starts running it
      if (step.is_draft) onUpdate?.(step, { is_draft: false })

      // backend creates the task with a ticket, output & status arrive by
      // stream; statuses like submitted/building stay RUNNING until terminal rows
      if (!data.step_id || data.status === 'failed') {
        // backend raises 4xx/5xx when submit fails, so this branch is an
        // unexpected case, kept to hold the contract for future flows
        onUpdate?.(step, {
          status: STEP_STATUS.SUCCESS,
          raw_log: cleanBytesOutput(data.raw_output ?? ''),
        })
        return data
      }

      const controller = new AbortController()
      let resolved = false
      let closing = false
      let failed = false

      // terminal status arrives before the output row; stay open briefly so
      // the output is not wiped, then give up if nothing else comes
      const closeSoon = () => {
        if (closing) return
        closing = true
        setTimeout(() => {
          if (resolved) return
          resolved = true
          controller.abort()
        }, 800)
      }

      await stepApi
        .streamStepEvents(
          data.step_id,
          (event) => {
            if (resolved) return
            const kind = event.event
            if (kind === 'snapshot') {
              // fast commands can finish before the stream attaches
              const snap = event.step
              if (snap && (snap.status === 'success' || FAILED_STATUSES.has(snap.status))) {
                const out =
                  (snap.bash_tasks || []).find((t) => t.output)?.output ??
                  (snap.tasks || []).find((t) => t.output)?.output
                if (out !== undefined) {
                  resolved = true
                  controller.abort()
                  onUpdate?.(step, {
                    status: snap.status === 'success' ? STEP_STATUS.SUCCESS : STEP_STATUS.ERROR,
                    raw_log: cleanBytesOutput(out),
                  })
                  return
                }
              }
              return
            }
            if (kind === 'output') {
              resolved = true
              controller.abort()
              onUpdate?.(step, {
                // mythic output rows carry no status, so keep the failure seen earlier
                status:
                  failed || FAILED_STATUSES.has(event.status)
                    ? STEP_STATUS.ERROR
                    : STEP_STATUS.SUCCESS,
                raw_log: cleanBytesOutput(event.raw_log ?? ''),
              })
              return
            }
            if (kind === 'build' && event.status) {
              onUpdate?.(step, {
                status: event.status === 'success' ? STEP_STATUS.SUCCESS : STEP_STATUS.RUNNING,
                raw_log: event.status === 'success' ? cleanBytesOutput(event.raw_log ?? '') : '',
              })
              if (event.status === 'success') {
                resolved = true
                controller.abort()
              }
              return
            }
            if (kind === 'status') {
              if (FAILED_STATUSES.has(event.status)) {
                // the output row comes after the terminal status: keep the
                // stream open so the real log lands, not the word "failed"
                failed = true
                onUpdate?.(step, { status: STEP_STATUS.ERROR })
                closeSoon()
                return
              }
              if (event.completed) {
                onUpdate?.(step, { status: STEP_STATUS.SUCCESS })
                closeSoon()
                return
              }
              onUpdate?.(step, { status: STEP_STATUS.RUNNING })
            }
          },
          controller.signal,
        )
        .catch((err) => {
          // abort after terminal rows is the expected way out, anything else
          // is a broken stream (drop, 4xx/5xx, dead backend), keep the state
          if (err?.name !== 'AbortError') {
            console.warn(`step ${data.step_id} events stream failed:`, err)
          }
        })

      return data
    } catch (err) {
      onUpdate?.(step, {
        status: STEP_STATUS.ERROR,
        raw_log: err?.response?.data?.detail ?? err.message,
      })
      throw err
    } finally {
      setRunningIds((prev) => {
        const next = new Set(prev)
        next.delete(id)
        return next
      })
    }
  }, [])

  return { run, runningIds }
}
