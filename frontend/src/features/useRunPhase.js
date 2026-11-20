import { useCallback } from 'react'
import { useRunStep } from './useRunStep'
import { runControl } from '../shared/runControl'

// stop has to break a 15 minute dwell too, so poll instead of one long timeout
const waitFor = (ms) =>
  new Promise((resolve) => {
    const started = Date.now()
    const tick = () => {
      if (runControl.stopped || Date.now() - started >= ms) resolve()
      else setTimeout(tick, 250)
    }
    tick()
  })

export const useRunPhase = (onUpdate, { timing = false } = {}) => {
  const { run: runStep, runningIds } = useRunStep(onUpdate)

  const run = useCallback(
    async (phaseName, chainId, steps) => {
      runControl.reset()

      // replay phase: every step with a command, except one already running
      const runnable = steps.filter(
        (step) => step.status !== 'running' && (step.command || step.command_params || '').trim(),
      )

      for (const [i, step] of runnable.entries()) {
        if (runControl.stopped) break
        const id = step.id ?? step._tempId
        const dwellMs =
          timing && step.delay_enabled !== false ? (step.delay_before_secs ?? 0) * 1000 : 0

        if (dwellMs > 0) {
          // the card counts down the same wall clock a chain run streams
          onUpdate?.(id, {
            wait_until: Date.now() + dwellMs,
            wait_seconds: Math.round(dwellMs / 1000),
          })
          await waitFor(dwellMs)
          onUpdate?.(id, { wait_until: null, wait_seconds: null })
        } else if (i > 0) {
          // run each step, after response next with little timeout for host
          await new Promise((resolve) => setTimeout(resolve, 700))
        }
        if (runControl.stopped) break
        await runStep(step, phaseName, chainId, onUpdate)
      }
    },
    [runStep, onUpdate, timing],
  )

  return { run, runningIds }
}
