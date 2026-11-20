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

export const ChainCanvas = ({
  currentChain,
  onSetPhase,
  reloadToken = 0,
  layout = 'columns',
  timing,
  onTimingSummary,
}) => {
  const tempIdRef = useRef(0)

  const [phases, setPhases] = useState(() => {
    const phase = UCKC_PHASES.find((p) => p.name === currentChain.current_phase_name)
    return phase ? [phase] : []
  })
  const [widths, setWidths] = useState(() => {
    const phase = UCKC_PHASES.find((p) => p.name === currentChain.current_phase_name)
    return phase ? { [phase.id]: 420 } : {}
  })
  const [stepsByPhase, setStepsByPhase] = useState({})
  // updateStep needs to know whether a step exists in the DB; keep the latest lists
  const stepsRef = useRef(stepsByPhase)

  useEffect(() => {
    if (!currentChain) return

    const loadSteps = async () => {
      try {
        const data = await chainApi.getChainWithSteps(currentChain.chain_id)
        const stepsByPhaseMap = {}
        const phasesFromSteps = []

        for (const step of data.steps) {
          const phaseName = step.phase
          const phaseObj = UCKC_PHASES.find((p) => p.name === phaseName)
          const phaseId = phaseObj ? phaseObj.id : phaseName
          const isAgent = step.step_type === 'agent'
          const rawType = step.step_type ?? STEP_TYPES.LOCAL
          if (!stepsByPhaseMap[phaseId]) {
            stepsByPhaseMap[phaseId] = []
            if (phaseObj) phasesFromSteps.push(phaseObj)
          }

          stepsByPhaseMap[phaseId].push({
            id: step.id,
            chain_name: currentChain.chain_name,
            step_type: rawType,
            callback_display_id: step.callback_display_id ?? 0,
            tool_name: step.tool_name,
            command: isAgent ? '' : step.command,
            command_params: isAgent ? step.command : '',
            tool: isAgent ? step.tool_name : 'shell',
            status: step.status,
            raw_log: step.raw_log,
            delay_before_secs: step.delay_before_secs ?? 0,
            delay_reason: step.delay_reason ?? '',
            delay_enabled: step.delay_enabled ?? true,
          })
        }

        setPhases((prev) => {
          const existingIds = new Set(prev.map((p) => p.id))
          const newPhases = phasesFromSteps.filter((p) => !existingIds.has(p.id))
          if (newPhases.length === 0) return prev
          return [...prev, ...newPhases].sort((a, b) => a.order - b.order)
        })

        setStepsByPhase((prev) => {
          const hasExistingSteps = Object.values(prev).some((s) => s.length > 0)
          if (hasExistingSteps) return prev
          return stepsByPhaseMap
        })
      } catch {
        toast.error('Could not load steps for this chain')
      }
    }
    loadSteps()
  }, [currentChain])

  useEffect(() => {
    stepsRef.current = stepsByPhase
  }, [stepsByPhase])

  // Agent proposals replace unexecuted rows in the DB; refetch after an import
  // instead of guessing, and keep not-yet-saved local steps on top.
  useEffect(() => {
    if (!reloadToken || !currentChain) return

    let alive = true
    const refresh = async () => {
      try {
        const data = await chainApi.getChainWithSteps(currentChain.chain_id)
        if (!alive) return

        const map = {}
        const phasesFromSteps = []

        for (const step of data.steps) {
          const phaseObj = UCKC_PHASES.find((p) => p.name === step.phase)
          const phaseId = phaseObj ? phaseObj.id : step.phase
          const isAgent = step.step_type === 'agent'
          if (!map[phaseId]) {
            map[phaseId] = []
            if (phaseObj) phasesFromSteps.push(phaseObj)
          }
          map[phaseId].push({
            id: step.id,
            chain_name: currentChain.chain_name,
            step_type: step.step_type ?? STEP_TYPES.LOCAL,
            callback_display_id: step.callback_display_id ?? 0,
            tool_name: step.tool_name,
            command: isAgent ? '' : step.command,
            command_params: isAgent ? step.command : '',
            tool: isAgent ? step.tool_name : 'shell',
            status: step.status,
            raw_log: step.raw_log,
            delay_before_secs: step.delay_before_secs ?? 0,
            delay_reason: step.delay_reason ?? '',
            delay_enabled: step.delay_enabled ?? true,
          })
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
      } catch {
        toast.error('Could not refresh the chain')
      }
    }
    refresh()

    return () => {
      alive = false
    }
  }, [reloadToken, currentChain])

  const updateStep = useCallback(async (stepId, patch, localOnly = false) => {
    setStepsByPhase((prev) => {
      const next = { ...prev }
      for (const phase of Object.keys(next)) {
        next[phase] = next[phase].map((s) =>
          s.id === stepId || s._tempId === stepId ? { ...s, ...patch } : s,
        )
      }
      return next
    })

    const isLocalOnly =
      localOnly ||
      Object.values(stepsRef.current).some((list) => list.some((step) => step._tempId === stepId))
    // status/output belong to the backend: a late PATCH("running") must never
    // overwrite the worker's "success". Only user edits are persisted.
    const isEdit =
      patch.command !== undefined ||
      patch.tool_name !== undefined ||
      patch.step_type !== undefined ||
      patch.callback_display_id !== undefined

    // dwell edits persist as well, but they must not move the card
    const isTiming =
      patch.delay_before_secs !== undefined ||
      patch.delay_reason !== undefined ||
      patch.delay_enabled !== undefined

    // temp/imported steps have no DB row yet; a PATCH would 404 them
    if (!isLocalOnly && (isEdit || isTiming)) {
      try {
        await stepApi.updateStep(stepId, patch)
        // only an edited command/tool changes the chain order; status and
        // output patches must not move the card
        if (isEdit) {
          setStepsByPhase((prev) => {
            const next = { ...prev }
            for (const phase of Object.keys(next)) {
              const list = next[phase]
              const idx = list.findIndex((s) => s.id === stepId || s._tempId === stepId)
              if (idx !== -1) {
                const [moved] = list.splice(idx, 1)
                next[phase] = [...list, moved]
                break
              }
            }
            return next
          })
        }

        if (isEdit && (patch.command !== undefined || patch.tool_name !== undefined)) {
          toast.info('Step moved to the end of its phase')
        }
      } catch {
        toast.error('Could not save the step')
      }
    }
  }, [])

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

  const addStep = useCallback(
    (phaseId) => {
      tempIdRef.current += 1

      // default API vars for run-command's
      const newStep = {
        _tempId: tempIdRef.current,
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
    async (stepId) => {
      const isTemp = !Object.values(stepsByPhase).some((steps) =>
        steps.some((s) => s.id === stepId && s.id != null),
      )
      if (!isTemp) {
        try {
          await stepApi.deleteStep(stepId)
        } catch {
          toast.error('Could not delete the step')
        }
      }

      setStepsByPhase((prev) => {
        const next = { ...prev }
        for (const phase of Object.keys(next)) {
          next[phase] = next[phase].filter((s) => s.id !== stepId && s._tempId !== stepId)
        }
        return next
      })
    },
    [stepsByPhase],
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
            onAddStep={() => addStep(phase.id)}
            onUpdateStep={updateStep}
            onDeleteStep={deleteStep}
            onRunStep={(step) => run(step, phase.name, currentChain.chain_id, updateStep)}
            onCopyStep={copyStep}
            onRunPhase={() =>
              runPhase(phase.name, currentChain.chain_id, stepsByPhase[phase.id] ?? [])
            }
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
