import { useState, useCallback, useRef, useEffect } from 'react'
import { Column } from '../../widgets/Column'
import { UCKC_PHASES } from '../../shared/config/mitre'
import { STEP_STATUS, STEP_TYPES } from '../../entities/types'
import { chainApi } from '../../entities/chainApi'
import { stepApi } from '../../entities/stepApi'
import { useRunStep } from '../../features/useRunStep'
import { useRunPhase } from '../../features/useRunPhase'
import { useAddPhase } from '../../features/useAddPhase'
import { toast } from 'react-toastify'
import {
  TEXT_FIELDS,
  cardCommand,
  cardTool,
  mapServerStep,
  sameStep,
  toApiPatch,
} from './stepMapping'

const SAVE_DEBOUNCE_MS = 350

export const ChainCanvas = ({
  currentChain,
  onSetPhase,
  reloadToken = 0,
  layout = 'columns',
  timing,
  onTimingSummary,
}) => {
  const tempIdRef = useRef(0)
  const deletedTempsRef = useRef(new Set())
  const creatingRef = useRef(new Map())
  const pendingSavesRef = useRef(new Map())

  const [phases, setPhases] = useState(() => {
    const phase = UCKC_PHASES.find((p) => p.name === currentChain.current_phase_name)
    return phase ? [phase] : []
  })
  const [widths, setWidths] = useState(() => {
    const phase = UCKC_PHASES.find((p) => p.name === currentChain.current_phase_name)
    return phase ? { [phase.id]: 420 } : {}
  })
  const [stepsByPhase, setStepsByPhase] = useState({})
  const stepsRef = useRef(stepsByPhase)

  // server state wins on load/refresh; local cards without a command stay on top
  const applyServerSteps = useCallback(
    (serverSteps) => {
      if (!currentChain) return

      const map = {}
      const phasesFromSteps = []

      for (const step of serverSteps) {
        const phaseObj = UCKC_PHASES.find((p) => p.name === step.phase)
        const phaseId = phaseObj ? phaseObj.id : step.phase
        if (!map[phaseId]) {
          map[phaseId] = []
          if (phaseObj) phasesFromSteps.push(phaseObj)
        }
        map[phaseId].push(mapServerStep(step, currentChain.chain_name))
      }

      setPhases((prev) => {
        const known = new Set(prev.map((p) => p.id))
        const added = phasesFromSteps.filter((p) => !known.has(p.id))
        return added.length ? [...prev, ...added].sort((a, b) => a.order - b.order) : prev
      })

      setWidths((prev) => {
        const next = { ...prev }
        for (const phase of phasesFromSteps) {
          if (next[phase.id] == null) next[phase.id] = 420
        }
        return next
      })

      setStepsByPhase((prev) => {
        const localOnly = {}
        for (const [phaseId, list] of Object.entries(prev)) {
          const kept = list.filter((step) => step.id == null)
          if (kept.length) localOnly[phaseId] = kept
        }
        const merged = { ...map }
        for (const [phaseId, list] of Object.entries(localOnly)) {
          merged[phaseId] = [...list, ...(merged[phaseId] ?? [])]
        }
        return merged
      })
    },
    [currentChain],
  )

  useEffect(() => {
    if (!currentChain) return

    let alive = true
    chainApi
      .getChainWithSteps(currentChain.chain_id)
      .then((data) => {
        if (alive) applyServerSteps(data.steps)
      })
      .catch(() => {
        if (alive) toast.error('Could not load steps for this chain')
      })

    return () => {
      alive = false
    }
  }, [currentChain, applyServerSteps])

  useEffect(() => {
    stepsRef.current = stepsByPhase
  }, [stepsByPhase])

  useEffect(
    () => () => {
      for (const entry of pendingSavesRef.current.values()) clearTimeout(entry.timer)
    },
    [],
  )

  // Agent proposals replace unexecuted rows in the DB; refetch after an import
  // instead of guessing, and keep not-yet-saved local steps on top.
  useEffect(() => {
    if (!reloadToken || !currentChain) return

    let alive = true
    chainApi
      .getChainWithSteps(currentChain.chain_id)
      .then((data) => {
        if (alive) applyServerSteps(data.steps)
      })
      .catch(() => {
        if (alive) toast.error('Could not refresh the chain')
      })

    return () => {
      alive = false
    }
  }, [reloadToken, currentChain, applyServerSteps])

  const findStep = useCallback((target) => {
    if (target && typeof target === 'object') return target
    for (const list of Object.values(stepsRef.current)) {
      const found = list.find((step) => step.id === target || step._tempId === target)
      if (found) return found
    }
    return null
  }, [])

  const findLocalStep = useCallback((tempId) => {
    for (const list of Object.values(stepsRef.current)) {
      const found = list.find((step) => step.id == null && step._tempId === tempId)
      if (found) return found
    }
    return null
  }, [])

  const moveToEnd = useCallback((step) => {
    let moved = false
    for (const list of Object.values(stepsRef.current)) {
      const idx = list.findIndex((s) => sameStep(s, step))
      if (idx !== -1) {
        moved = idx !== list.length - 1
        break
      }
    }

    setStepsByPhase((prev) => {
      const next = {}
      for (const [phaseId, list] of Object.entries(prev)) {
        const idx = list.findIndex((s) => sameStep(s, step))
        next[phaseId] =
          idx === -1 ? list : [...list.slice(0, idx), ...list.slice(idx + 1), list[idx]]
      }
      return next
    })
    return moved
  }, [])

  const persistPatch = useCallback(
    async (stepId, patch, isEdit) => {
      if (!currentChain) return

      // read the freshest row: a run may promote the draft while a save is queued
      const step = findStep(stepId)
      if (!step || step.id == null) return

      try {
        if (step.is_draft) {
          await stepApi.updateDraftStep(currentChain.chain_id, step.id, patch)
        } else {
          await stepApi.updateStep(step.id, patch)
        }
        // only an edited command/tool changes the chain order; status and
        // output patches must not move the card
        if (isEdit && moveToEnd(step)) {
          toast.info('Step moved to the end of its phase')
        }
      } catch {
        toast.error('Could not save the step')
      }
    },
    [currentChain, findStep, moveToEnd],
  )

  const flushStep = useCallback(
    async (stepId) => {
      const entry = pendingSavesRef.current.get(stepId)
      if (!entry) return

      clearTimeout(entry.timer)
      pendingSavesRef.current.delete(stepId)
      await persistPatch(stepId, entry.patch, entry.isEdit)
    },
    [persistPatch],
  )

  const createLocalDraft = useCallback(
    (localStep) => {
      const tempId = localStep._tempId
      const inFlight = creatingRef.current.get(tempId)
      if (inFlight) return inFlight

      const promise = (async () => {
        try {
          const created = await stepApi.createDraftStep(currentChain.chain_id, {
            phase: localStep.phase,
            step_type: localStep.step_type,
            tool_name: cardTool(localStep),
            command: cardCommand(localStep),
            delay_before_secs: localStep.delay_before_secs ?? 0,
          })

          if (deletedTempsRef.current.has(tempId)) {
            await stepApi.deleteDraftStep(currentChain.chain_id, created.id).catch(() => {})
            return null
          }

          const serverStep = {
            ...mapServerStep(created, currentChain.chain_name),
            // stable React key across the local -> server swap, keeps focus
            _tempId: tempId,
            callback_display_id: localStep.callback_display_id ?? 0,
          }
          setStepsByPhase((prev) => {
            const next = {}
            for (const [phaseId, list] of Object.entries(prev)) {
              next[phaseId] = list
                .filter((step) => step.id !== serverStep.id)
                .map((step) => (step.id == null && step._tempId === tempId ? serverStep : step))
            }
            return next
          })

          // edits that landed while the POST was in flight
          const latest = findLocalStep(tempId) ?? serverStep
          const sync = {}
          const latestCommand = cardCommand(latest)
          if (latestCommand.trim() && latestCommand !== cardCommand(serverStep)) {
            sync.command = latestCommand
          }
          const latestTool = cardTool(latest)
          if (latestTool && latestTool !== cardTool(serverStep)) sync.tool_name = latestTool
          if ((latest.delay_before_secs ?? 0) !== (serverStep.delay_before_secs ?? 0)) {
            sync.delay_before_secs = latest.delay_before_secs ?? 0
          }
          if (latest.delay_reason) sync.delay_reason = latest.delay_reason
          if (latest.delay_enabled === false) sync.delay_enabled = false

          if (Object.keys(sync).length) {
            try {
              await stepApi.updateDraftStep(currentChain.chain_id, created.id, sync)
            } catch {
              toast.error('Could not save the step')
            }
          }
          return serverStep
        } catch {
          toast.error('Could not create the step')
          return null
        } finally {
          creatingRef.current.delete(tempId)
        }
      })()

      creatingRef.current.set(tempId, promise)
      return promise
    },
    [currentChain, findLocalStep],
  )

  const updateStep = useCallback(
    async (target, patch = {}) => {
      const step = findStep(target)
      if (!step) return

      setStepsByPhase((prev) => {
        const next = {}
        for (const [phaseId, list] of Object.entries(prev)) {
          next[phaseId] = list.map((s) => (sameStep(s, step) ? { ...s, ...patch } : s))
        }
        return next
      })

      // status/output belong to the backend: a late PATCH("running") must never
      // overwrite the worker's "success". Only user edits are persisted.
      const isEdit =
        patch.command !== undefined ||
        patch.tool_name !== undefined ||
        patch.step_type !== undefined ||
        // agent cards edit the same db columns under their own names
        patch.command_params !== undefined ||
        patch.tool !== undefined

      // dwell edits persist as well, but they must not move the card
      const isTiming =
        patch.delay_before_secs !== undefined ||
        patch.delay_reason !== undefined ||
        patch.delay_enabled !== undefined

      if (!isEdit && !isTiming) return

      const apiPatch = toApiPatch(patch)
      if (Object.keys(apiPatch).length === 0) return

      const merged = { ...step, ...patch }
      if (merged.id == null) {
        // the draft API rejects an empty command, so the row appears on the
        // first keystroke instead of at add time
        if (cardCommand(merged).trim()) createLocalDraft(merged)
        return
      }

      const hasText = Object.keys(apiPatch).some((field) => TEXT_FIELDS.has(field))
      if (!hasText) {
        await persistPatch(merged.id, apiPatch, isEdit)
        return
      }

      const pending = pendingSavesRef.current.get(merged.id)
      if (pending) clearTimeout(pending.timer)
      const mergedPatch = { ...(pending?.patch ?? {}), ...apiPatch }
      const timer = setTimeout(() => {
        pendingSavesRef.current.delete(merged.id)
        persistPatch(merged.id, mergedPatch, pending?.isEdit || isEdit)
      }, SAVE_DEBOUNCE_MS)
      pendingSavesRef.current.set(merged.id, {
        patch: mergedPatch,
        timer,
        isEdit: pending?.isEdit || isEdit,
      })
    },
    [findStep, createLocalDraft, persistPatch],
  )

  // one request flips dwell for every step in the column
  const togglePhaseTiming = useCallback(
    async (phaseName, enabled) => {
      if (!currentChain) return
      const phaseId = UCKC_PHASES.find((p) => p.name === phaseName)?.id ?? phaseName

      setStepsByPhase((prev) => {
        const list = prev[phaseId]
        if (!list) return prev
        return {
          ...prev,
          [phaseId]: list.map((step) => ({ ...step, delay_enabled: enabled })),
        }
      })

      try {
        await stepApi.updatePhaseTiming(currentChain.chain_id, phaseName, enabled)
      } catch {
        toast.error('Could not change timing for the phase')
      }
    },
    [currentChain],
  )

  // the agent panel shows what enabling timing would cost before it is enabled
  useEffect(() => {
    const steps = Object.values(stepsByPhase).flat()
    const plannedSecs = steps.reduce(
      (sum, step) => sum + (step.delay_enabled === false ? 0 : (step.delay_before_secs ?? 0)),
      0,
    )
    onTimingSummary?.({ plannedSecs, steps: steps.length })
  }, [stepsByPhase, onTimingSummary])

  const { run } = useRunStep()
  const { run: runPhase } = useRunPhase(updateStep, { timing: timing?.enabled })
  const { addPhase: handleAddPhase } = useAddPhase(currentChain?.chain_id ?? null)

  const reloadFromServer = useCallback(async () => {
    if (!currentChain) return
    const data = await chainApi.getChainWithSteps(currentChain.chain_id)
    applyServerSteps(data.steps)
  }, [currentChain, applyServerSteps])

  const runSingleStep = useCallback(
    async (step, phaseName) => {
      if (!currentChain) return

      let ready = step
      if (ready.id == null && ready._tempId != null) {
        // wait for the draft row so the run always targets a stored step
        ready = await creatingRef.current.get(ready._tempId)
      }
      if (!ready || ready.id == null) return

      // the run reads the command from the db, so a queued edit must land first
      await flushStep(ready.id)
      // failures are already written into the card by useRunStep
      run(ready, phaseName, currentChain.chain_id, updateStep).catch(() => {})
    },
    [currentChain, flushStep, run, updateStep],
  )

  const addStep = useCallback(
    (phaseId, phaseName) => {
      tempIdRef.current += 1

      const newStep = {
        id: null,
        _tempId: tempIdRef.current,
        is_draft: true,
        phase: phaseName,
        chain_name: currentChain?.chain_name ?? '',
        step_type: STEP_TYPES.LOCAL,
        callback_display_id: 0,
        tool_name: '',
        command: '',
        command_params: '',
        tool: 'shell',
        status: STEP_STATUS.PENDING,
        raw_log: '',
        delay_before_secs: 0,
        delay_reason: '',
        delay_enabled: true,
      }

      setStepsByPhase((prev) => ({
        ...prev,
        [phaseId]: [...(prev[phaseId] ?? []), newStep],
      }))
    },
    [currentChain],
  )

  const deleteStep = useCallback(
    async (target) => {
      const step = findStep(target)
      if (!step) return

      const pending = step.id != null ? pendingSavesRef.current.get(step.id) : null
      if (pending) {
        clearTimeout(pending.timer)
        pendingSavesRef.current.delete(step.id)
      }
      if (step.id == null) deletedTempsRef.current.add(step._tempId)

      setStepsByPhase((prev) => {
        const next = {}
        for (const [phaseId, list] of Object.entries(prev)) {
          next[phaseId] = list.filter((s) => !sameStep(s, step))
        }
        return next
      })

      if (step.id == null) return

      try {
        if (step.is_draft) {
          await stepApi.deleteDraftStep(currentChain.chain_id, step.id)
        } else {
          await stepApi.deleteStep(step.id)
        }
      } catch (err) {
        if (err?.response?.status === 404) {
          // already gone; the canvas only knows what the server says
          try {
            await reloadFromServer()
          } catch {
            toast.error('Could not refresh the chain')
          }
        } else {
          toast.error('Could not delete the step')
        }
      }
    },
    [findStep, currentChain, reloadFromServer],
  )
  // for now just copy logs
  const copyStep = (step) => {
    const text = `Type: ${step.step_type}\nTool: ${step.tool_name}\nID: ${step.callback_display_id}\nStatus: ${step.status}\n\n${step.step_type === 'agent' ? 'Params' : 'Command'}:\n${step.step_type === 'agent' ? step.command_params : step.command}\n\nOutput:\n${step.raw_log}`
    navigator.clipboard.writeText(text)
  }
  // Backend don't know about phases, it know current phase for step,
  // so if phase without runned steps, then should be store on frontend side for now
  const removePhase = useCallback((phaseId) => {
    if (!window.confirm('Remove this phase?')) return

    setPhases((prev) => prev.filter((p) => p.id !== phaseId))
    setStepsByPhase((prev) => {
      const { [phaseId]: _, ...rest } = prev
      return rest
    })
  }, [])

  const insertPhaseAfter = useCallback((afterId, newPhaseId) => {
    const newPhase = UCKC_PHASES.find((p) => p.id === newPhaseId)
    if (!newPhase) return

    setPhases((prev) => {
      const idx = prev.findIndex((p) => p.id === afterId)
      if (idx === -1) return prev
      return [...prev.slice(0, idx + 1), newPhase, ...prev.slice(idx + 1)]
    })
    setWidths((prev) => ({ ...prev, [newPhaseId]: 420 }))
  }, [])

  const insertPhaseBefore = useCallback((beforeId, newPhaseId) => {
    const newPhase = UCKC_PHASES.find((p) => p.id === newPhaseId)
    if (!newPhase) return

    setPhases((prev) => {
      const idx = prev.findIndex((p) => p.id === beforeId)
      if (idx === -1) return prev
      return [...prev.slice(0, idx), newPhase, ...prev.slice(idx)]
    })
    setWidths((prev) => ({ ...prev, [newPhaseId]: 420 }))
  }, [])

  const onInsertPhase = useCallback(
    async (originId, newPhaseId) => {
      const newPhase = UCKC_PHASES.find((p) => p.id === newPhaseId)
      if (!newPhase || !currentChain) return
      try {
        await handleAddPhase(newPhase.name)
      } catch {
        return
      }

      insertPhaseAfter(originId, newPhaseId)
    },
    [currentChain, handleAddPhase, insertPhaseAfter],
  )

  const onInsertPhaseBefore = useCallback(
    async (originId, newPhaseId) => {
      const newPhase = UCKC_PHASES.find((p) => p.id === newPhaseId)
      if (!newPhase || !currentChain) return
      try {
        await handleAddPhase(newPhase.name)
      } catch {
        return
      }

      insertPhaseBefore(originId, newPhaseId)
    },
    [currentChain, handleAddPhase, insertPhaseBefore],
  )

  const existingOrders = phases.map((p) => p.order)

  return (
    <main className="flex-1 overflow-auto p-4">
      <div
        className={
          layout === 'rows'
            ? 'mx-auto flex w-full max-w-[1100px] flex-col gap-3'
            : 'flex min-w-max items-start gap-4'
        }
      >
        {phases.map((phase, idx) => (
          <Column
            key={phase.id}
            phase={phase}
            index={idx}
            steps={stepsByPhase[phase.id] ?? []}
            width={widths[phase.id] ?? 420}
            onWidthChange={(w) => setWidths((prev) => ({ ...prev, [phase.id]: w }))}
            onAddStep={() => addStep(phase.id, phase.name)}
            onUpdateStep={updateStep}
            onDeleteStep={deleteStep}
            onRunStep={(step) => runSingleStep(step, phase.name)}
            onCopyStep={copyStep}
            onRunPhase={async () => {
              const steps = stepsByPhase[phase.id] ?? []
              // queued text edits must reach the db before their step is replayed
              await Promise.all(steps.filter((s) => s.id != null).map((s) => flushStep(s.id)))
              runPhase(phase.name, currentChain.chain_id, steps).catch(() => {})
            }}
            onInsertPhase={onInsertPhase}
            onInsertPhaseBefore={onInsertPhaseBefore}
            onRemovePhase={() => removePhase(phase.id)}
            onSetPhase={onSetPhase}
            isActive={phase.name === currentChain.current_phase_name}
            isLast={idx === phases.length - 1}
            stacked={layout === 'rows'}
            existingOrders={existingOrders}
            timing={timing}
            onTogglePhaseTiming={(enabled) => togglePhaseTiming(phase.name, enabled)}
          />
        ))}
      </div>
    </main>
  )
}
