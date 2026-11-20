import { useRef, useEffect, useState } from 'react'
import { STEP_TYPES, STEP_STATUS } from '../entities/types'
import { ArrowClockwiseIcon, PlayIcon } from '@phosphor-icons/react'
import { useMinify } from '../context/useMinify.js'
import {
  Input,
  TextInput,
  TransparentInput,
  Textarea,
  ReadonlyTextarea,
  ActionButton,
  RunButton,
} from './UIComponents'

const statusMeta = {
  planned: { label: 'planned', cls: 'text-accent' },
  pending: { label: 'pending', cls: 'text-muted' },
  submitted: { label: 'submitted', cls: 'text-muted' },
  running: { label: 'running', cls: 'text-warning' },
  success: { label: 'success', cls: 'text-success' },
  error: { label: 'error', cls: 'text-danger' },
  // backend bash uses fail/failed; without these the card falls back to pending
  fail: { label: 'failed', cls: 'text-danger' },
  failed: { label: 'failed', cls: 'text-danger' },
  rejected: { label: 'rejected', cls: 'text-muted line-through' },
}

const FieldLabel = ({ children }) => <div className="mb-1 text-[11px] text-muted">{children}</div>

// per-step textarea heights, persisted by chain + step id (local, UI-only)
const HEIGHT_KEY = 'eagle_step_heights_v1'
const MIN_HEIGHT = 56
const MAX_HEIGHT = 600
let heightCache = null

const loadHeights = () => {
  if (heightCache) return heightCache

  try {
    heightCache = JSON.parse(localStorage.getItem(HEIGHT_KEY) || '{}')
  } catch {
    heightCache = {}
  }

  return heightCache
}

const readHeight = (key, fallback) => {
  const value = loadHeights()[key]
  return Number.isFinite(value) ? value : fallback
}

const saveHeight = (key, value) => {
  const all = loadHeights()
  all[key] = value

  const keys = Object.keys(all)
  if (keys.length > 300) delete all[keys[0]]
  localStorage.setItem(HEIGHT_KEY, JSON.stringify(all))
}

const ResizeHandle = ({ onStart, onFit, title }) => (
  <div
    onMouseDown={onStart}
    onDoubleClick={onFit}
    title={title}
    className="group flex h-3 cursor-row-resize items-center justify-center"
  >
    <span className="h-px w-8 bg-subtle transition-colors group-hover:bg-default" />
  </div>
)

export const StepCard = ({ step, ordinal, onRun, onDelete, onCopy, onUpdate, timing }) => {
  const cmdRef = useRef(null)
  const outRef = useRef(null)
  const heightKey = `${step.chain_name}:${step.id ?? step._tempId}`
  const [cmdHeight, setCmdHeight] = useState(() => readHeight(`${heightKey}:cmd`, 88))
  const [outHeight, setOutHeight] = useState(() => readHeight(`${heightKey}:out`, 180))

  const { minified } = useMinify()
  const [elapsed, setElapsed] = useState(0)
  const [waitNow, setWaitNow] = useState(() => Date.now())
  const startTimeRef = useRef(null)
  const intervalRef = useRef(null)
  const isAgent = step.step_type === STEP_TYPES.AGENT
  const isBash = step.step_type === STEP_TYPES.BASH
  const delayMinutes = Math.round((step.delay_before_secs ?? 0) / 60)

  // phase runs count down locally; chain runs stream the same wall clock
  useEffect(() => {
    if (!step.wait_until) return

    const id = setInterval(() => setWaitNow(Date.now()), 250)
    return () => clearInterval(id)
  }, [step.wait_until])

  const waitLeft = step.wait_until
    ? Math.max(
        0,
        Math.min(
          step.wait_seconds ?? Number.MAX_SAFE_INTEGER,
          Math.round((step.wait_until - waitNow) / 1000),
        ),
      )
    : 0

  // backend drives run state by events stream, here just elapsed clock
  useEffect(() => {
    if (step.status === STEP_STATUS.RUNNING) {
      startTimeRef.current = Date.now()
      intervalRef.current = setInterval(() => {
        setElapsed(Math.floor((Date.now() - startTimeRef.current) / 1000))
      }, 1000)
    } else {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }
  }, [step.status])

  // no saved height yet: open tall enough for the whole output, capped
  useEffect(() => {
    queueMicrotask(() => {
      if (minified) return

      const output = outRef.current
      if (output && loadHeights()[`${heightKey}:out`] === undefined) {
        setOutHeight(Math.max(180, Math.min(MAX_HEIGHT, output.scrollHeight + 4)))
      }

      const commandBox = cmdRef.current
      if (commandBox && loadHeights()[`${heightKey}:cmd`] === undefined) {
        setCmdHeight(Math.max(88, Math.min(MAX_HEIGHT, commandBox.scrollHeight + 4)))
      }
    })
  }, [minified, step.raw_log, step.command, step.command_params, heightKey])

  // drag the hairline under a textarea; double-click fits the content
  const startResize = (field) => (event) => {
    event.preventDefault()
    event.stopPropagation()
    const startY = event.clientY
    const startHeight = field === 'cmd' ? cmdHeight : outHeight
    const apply = (value) => (field === 'cmd' ? setCmdHeight(value) : setOutHeight(value))
    let latest = startHeight

    const onMove = (moveEvent) => {
      const next = Math.max(
        MIN_HEIGHT,
        Math.min(MAX_HEIGHT, startHeight + moveEvent.clientY - startY),
      )
      latest = next
      apply(next)
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      saveHeight(`${heightKey}:${field}`, latest)
    }

    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    document.body.style.cursor = 'row-resize'
    document.body.style.userSelect = 'none'
  }

  const fitHeight = (field) => (event) => {
    event.stopPropagation()
    const element = field === 'cmd' ? cmdRef.current : outRef.current
    if (!element) return

    const next = Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, element.scrollHeight + 4))
    if (field === 'cmd') setCmdHeight(next)
    else setOutHeight(next)
    saveHeight(`${heightKey}:${field}`, next)
  }

  const meta = statusMeta[step.status] ?? statusMeta.pending
  const isPlanned = step.status === 'planned'
  const command = (isAgent ? step.command_params : step.command) ?? ''
  const unexecuted = step.status === 'pending' || isPlanned
  // executed steps can be replayed in place (same row, refreshed output)
  const canRun = step.status !== 'running' && command.trim().length > 0

  return (
    <div
      className={`mb-2.5 rounded-lg border bg-card p-3 transition-colors hover:border-default ${
        isPlanned ? 'border-dashed border-default' : 'border-subtle'
      }`}
    >
      {/* card types depends on proc.py */}
      <div className="mb-2.5 flex items-center gap-2">
        <Input
          as="select"
          value={step.step_type === STEP_TYPES.BASH ? STEP_TYPES.LOCAL : step.step_type}
          onChange={(e) => onUpdate(step.id ?? step._tempId, { step_type: e.target.value })}
          className="h-7 w-[112px] px-1.5 text-[11px]"
        >
          <option value={STEP_TYPES.LOCAL}>Local (C2)</option>
          <option value={STEP_TYPES.AGENT}>Agent</option>
        </Input>

        <label className="flex items-center gap-1.5 text-[11px] text-muted">
          <input
            type="checkbox"
            checked={isBash}
            onChange={(e) =>
              onUpdate(step.id ?? step._tempId, {
                step_type: e.target.checked
                  ? STEP_TYPES.BASH
                  : isAgent
                    ? STEP_TYPES.AGENT
                    : STEP_TYPES.LOCAL,
              })
            }
            className="accent-current"
          />
          Bash on host
        </label>

        <span className={`ml-auto shrink-0 font-mono text-[11px] ${meta.cls}`}>
          {meta.label}
          {step.status === STEP_STATUS.RUNNING ? ` ${elapsed}s` : ''}
          {waitLeft > 0
            ? ` wait ${Math.floor(waitLeft / 60)}:${String(waitLeft % 60).padStart(2, '0')}`
            : ''}
        </span>
      </div>

      {!isBash && (
        <div className="mb-2 flex items-center gap-2">
          <span className="text-[11px] text-muted">Callback</span>
          <Input
            type="text"
            inputMode="numeric"
            value={step.callback_display_id ?? 0}
            onChange={(e) =>
              onUpdate(step.id ?? step._tempId, {
                callback_display_id: parseInt(e.target.value) || 0,
              })
            }
            className="h-7 w-16 px-1.5 font-mono text-[11px] [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
          />
        </div>
      )}

      {/* tool name depends on mythic agent tools and proc.py */}
      <TransparentInput
        type="text"
        value={step.tool_name ?? ''}
        onChange={(e) => onUpdate(step.id ?? step._tempId, { tool_name: e.target.value })}
        placeholder="Tool name"
      />

      {/* if runned on remote mythic agent */}
      {isAgent && (
        <div className="mb-2">
          <FieldLabel>Agent tool</FieldLabel>
          <TextInput
            type="text"
            value={step.tool ?? ''}
            onChange={(e) => onUpdate(step.id ?? step._tempId, { tool: e.target.value })}
            placeholder="shell, libinject…"
          />
        </div>
      )}

      {timing?.enabled && (
        <div className="mb-2 flex items-center gap-2">
          <label
            className="flex shrink-0 items-center gap-1.5 text-[11px] text-muted"
            title="Wait this long before running the step"
          >
            <input
              type="checkbox"
              checked={step.delay_enabled !== false}
              onChange={(event) =>
                onUpdate(step.id ?? step._tempId, { delay_enabled: event.target.checked })
              }
              className="accent-current"
            />
            Dwell
          </label>
          <Input
            type="text"
            inputMode="numeric"
            value={delayMinutes}
            onChange={(event) =>
              onUpdate(step.id ?? step._tempId, {
                delay_before_secs: (parseInt(event.target.value, 10) || 0) * 60,
              })
            }
            className="h-7 w-12 px-1.5 font-mono text-[11px] [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
          />
          <span className="shrink-0 text-[11px] text-muted">min</span>
          <Input
            type="text"
            value={step.delay_reason ?? ''}
            onChange={(event) =>
              onUpdate(step.id ?? step._tempId, { delay_reason: event.target.value })
            }
            placeholder="what the attacker did in that time"
            className="h-7 min-w-0 flex-1 px-1.5 text-[11px]"
          />
        </div>
      )}

      <FieldLabel>{isAgent ? 'Command params' : 'Command'}</FieldLabel>
      <Textarea
        ref={cmdRef}
        style={{ height: `${cmdHeight}px`, maxHeight: 'none', minHeight: `${MIN_HEIGHT}px` }}
        value={isAgent ? (step.command_params ?? '') : (step.command ?? '')}
        onChange={(e) =>
          onUpdate(step.id ?? step._tempId, {
            [isAgent ? 'command_params' : 'command']: e.target.value,
          })
        }
        placeholder={isAgent ? 'uname -a; lscpu; free' : 'Command to run'}
      />
      <ResizeHandle
        onStart={startResize('cmd')}
        onFit={fitHeight('cmd')}
        title="Drag to resize, double-click to fit"
      />

      {/* TODO: TTPs like kill chain view on minify, or by LLM analyze summary
          for now just disable output on minified context
      */}
      {!minified && (
        <>
          <div className="mt-2.5">
            <FieldLabel>Output</FieldLabel>
          </div>
          <ReadonlyTextarea
            ref={outRef}
            style={{ height: `${outHeight}px`, maxHeight: 'none', minHeight: `${MIN_HEIGHT}px` }}
            value={step.raw_log ?? ''}
            onChange={(e) => onUpdate(step.id ?? step._tempId, { raw_log: e.target.value })}
            placeholder="No output yet"
            readOnly
          />
          <ResizeHandle
            onStart={startResize('out')}
            onFit={fitHeight('out')}
            title="Drag to resize, double-click to fit"
          />
        </>
      )}

      <div className="mt-2.5 flex items-center gap-1 border-t border-subtle pt-2">
        <ActionButton
          hover="danger"
          onClick={(e) => {
            e.stopPropagation() // prevent other event handlers here
            if (!window.confirm('Delete this step?')) return
            onDelete(step.id ?? step._tempId)
          }}
        >
          Delete
        </ActionButton>
        <ActionButton
          onClick={(e) => {
            e.stopPropagation()
            onCopy(step)
          }}
        >
          Copy
        </ActionButton>

        {step.id && (
          <span
            className="ml-1 font-mono text-[11px] tabular-nums text-muted"
            title={`step id ${step.id}`}
          >
            {ordinal ?? step.id}
          </span>
        )}

        <RunButton
          className="ml-auto"
          onClick={(e) => {
            e.stopPropagation()
            onRun(step)
          }}
          disabled={step.status === 'running' || !canRun}
          title={canRun ? 'Run this step' : unexecuted ? 'No command yet' : 'Run in progress'}
        >
          {step.status === 'running' ? (
            <ArrowClockwiseIcon size={14} className="animate-spin" />
          ) : (
            <PlayIcon size={14} />
          )}
          Run
        </RunButton>
      </div>
    </div>
  )
}
