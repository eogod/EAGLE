import { useRef, useState, useEffect } from 'react'
import { StepCard } from './StepCard'
import { getAvailablePhasesAfter, getAvailablePhasesBefore } from '../shared/config/mitre'
import { MinusIcon, PlayIcon, PlusIcon, XIcon } from '@phosphor-icons/react'
import { IconButton, DashedIconButton, DropdownItem } from './UIComponents'

// for column resize
const MIN_WIDTH = 320
const MAX_WIDTH = 700
const DEFAULT_WIDTH = 420

export const Column = ({
  phase,
  index: _index,
  steps,
  width = DEFAULT_WIDTH,
  onWidthChange,
  onAddStep,
  onUpdateStep,
  onDeleteStep,
  onRunStep,
  onCopyStep,
  onRunPhase,
  onInsertPhase,
  onInsertPhaseBefore,
  onRemovePhase,
  onSetPhase,
  isActive,
  isLast,
  stacked = false,
  existingOrders,
  timing,
  onTogglePhaseTiming,
}) => {
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const [beforeDropdownOpen, setBeforeDropdownOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [beforeSearch, setBeforeSearch] = useState('')
  const dropdownRef = useRef(null)
  const beforeDropdownRef = useRef(null)

  const dwellSecs = steps.reduce(
    (sum, step) => sum + (step.delay_enabled === false ? 0 : (step.delay_before_secs ?? 0)),
    0,
  )
  const dwellOn = steps.length > 0 && steps.every((step) => step.delay_enabled !== false)

  useEffect(() => {
    if (!dropdownOpen) return

    const onClick = (e) => {
      // if click outside new Next phase dropdown menu then close
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [dropdownOpen])

  useEffect(() => {
    if (!beforeDropdownOpen) return

    const onClick = (e) => {
      // if click outside new Prev phase dropdown menu then close
      if (beforeDropdownRef.current && !beforeDropdownRef.current.contains(e.target)) {
        setBeforeDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [beforeDropdownOpen])

  // dynamic column resize for long commands and raw outputs, only for prev column on click
  const startResize = (e) => {
    e.preventDefault()
    const startX = e.clientX
    const startWidth = width

    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    const onMove = (ev) => {
      // check on correct phase column width
      const next = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, startWidth + (ev.clientX - startX)))
      onWidthChange(next)
    }
    const onUp = () => {
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }

    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  // check is new phase correct by UCKC
  const availablePhases = getAvailablePhasesAfter(phase.order, existingOrders)
  const filteredPhases = availablePhases.filter(
    (p) =>
      p.name.toLowerCase().includes(search.toLowerCase()) ||
      p.desc.toLowerCase().includes(search.toLowerCase()),
  )

  const availablePhasesBefore = getAvailablePhasesBefore(phase.order, existingOrders)
  const filteredPhasesBefore = availablePhasesBefore.filter(
    (p) =>
      p.name.toLowerCase().includes(beforeSearch.toLowerCase()) ||
      p.desc.toLowerCase().includes(beforeSearch.toLowerCase()),
  )

  return (
    <>
      <div
        className={stacked ? 'w-full cursor-pointer' : 'shrink-0 cursor-pointer'}
        style={stacked ? undefined : { width: `${width}px` }}
        onClick={() => onSetPhase(phase.name)}
      >
        <div
          className={`relative mb-3 flex items-center justify-between gap-2 rounded-md border border-subtle bg-card px-3 py-2 ${
            isActive ? 'border-l-2 border-l-accent border-y-subtle border-r-subtle' : ''
          }`}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex min-w-0 flex-1 items-baseline gap-2.5">
            <span className="shrink-0 font-mono text-[11px] tabular-nums text-muted">
              {String(phase.order).padStart(2, '0')}
            </span>
            <span
              className={`truncate text-[13px] ${isActive ? 'text-primary' : 'text-secondary'}`}
            >
              {phase.name}
            </span>

            {timing?.enabled && (
              <label
                className="ml-auto flex shrink-0 items-center gap-1 text-[11px] tabular-nums text-muted"
                title="Enable or disable dwell for this phase"
              >
                <input
                  type="checkbox"
                  checked={dwellOn}
                  onChange={(event) => onTogglePhaseTiming(event.target.checked)}
                  className="accent-current"
                />
                {Math.round(dwellSecs / 60)}m
              </label>
            )}
          </div>

          <div className="flex shrink-0 gap-0.5">
            <DashedIconButton
              onClick={() => setBeforeDropdownOpen((v) => !v)}
              title="Insert phase before"
            >
              <MinusIcon size={14} />
            </DashedIconButton>
            <DashedIconButton onClick={() => setDropdownOpen((v) => !v)} title="Insert phase after">
              <PlusIcon size={14} />
            </DashedIconButton>
            <IconButton onClick={onRunPhase} title="Run all steps in phase">
              <PlayIcon size={14} />
            </IconButton>
            <IconButton onClick={onRemovePhase} title="Remove phase">
              <XIcon size={14} />
            </IconButton>
          </div>

          {dropdownOpen && (
            <div
              ref={dropdownRef}
              className="absolute right-0 top-full z-50 mt-1 max-h-80 min-w-[280px] overflow-y-auto rounded-lg border border-subtle bg-dropdown shadow-2xl shadow-black/40"
            >
              <div className="sticky top-0 bg-dropdown px-3 py-2 text-[11px] text-muted">
                Insert after {phase.name}
              </div>
              <input
                autoFocus
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search phases"
                className="w-full border-y border-subtle bg-input px-3 py-2 text-xs text-primary outline-none placeholder:text-muted"
              />
              {filteredPhases.length === 0 ? (
                <div className="px-3 py-2 text-xs text-muted">No available phases</div>
              ) : (
                filteredPhases.map((p) => (
                  <DropdownItem
                    key={p.id}
                    onClick={() => {
                      onInsertPhase(phase.id, p.id)
                      setDropdownOpen(false)
                      setSearch('')
                    }}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="text-xs text-primary">{p.name}</div>
                      <div className="mt-0.5 text-[11px] leading-snug text-muted">{p.desc}</div>
                    </div>
                    <span className="text-muted">+</span>
                  </DropdownItem>
                ))
              )}
            </div>
          )}

          {beforeDropdownOpen && (
            <div
              ref={beforeDropdownRef}
              className="absolute right-0 top-full z-50 mt-1 max-h-80 min-w-[280px] overflow-y-auto rounded-lg border border-subtle bg-dropdown shadow-2xl shadow-black/40"
            >
              <div className="sticky top-0 bg-dropdown px-3 py-2 text-[11px] text-muted">
                Insert before {phase.name}
              </div>
              <input
                autoFocus
                type="text"
                value={beforeSearch}
                onChange={(e) => setBeforeSearch(e.target.value)}
                placeholder="Search phases"
                className="w-full border-y border-subtle bg-input px-3 py-2 text-xs text-primary outline-none placeholder:text-muted"
              />
              {filteredPhasesBefore.length === 0 ? (
                <div className="px-3 py-2 text-xs text-muted">No available phases</div>
              ) : (
                filteredPhasesBefore.map((p) => (
                  <DropdownItem
                    key={p.id}
                    onClick={() => {
                      onInsertPhaseBefore(phase.id, p.id)
                      setBeforeDropdownOpen(false)
                      setBeforeSearch('')
                    }}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="text-xs text-primary">{p.name}</div>
                      <div className="mt-0.5 text-[11px] leading-snug text-muted">{p.desc}</div>
                    </div>
                    <span className="text-muted">+</span>
                  </DropdownItem>
                ))
              )}
            </div>
          )}
        </div>

        <div className="space-y-0">
          {steps.length === 0 ? (
            <div className="rounded-md border border-dashed border-subtle px-4 py-6 text-center text-xs text-muted">
              No steps yet
            </div>
          ) : (
            steps.map((step, stepIndex) => (
              <StepCard
                key={step.id ?? step._tempId}
                step={step}
                ordinal={stepIndex + 1}
                onRun={onRunStep}
                onDelete={onDeleteStep}
                onCopy={onCopyStep}
                onUpdate={onUpdateStep}
                timing={timing}
              />
            ))
          )}
        </div>

        <button
          onClick={(e) => {
            e.stopPropagation()
            onAddStep()
          }}
          className="mt-2 flex h-9 w-full items-center justify-center gap-1.5 rounded-md border border-dashed border-subtle bg-transparent text-xs text-secondary transition-colors hover:border-default hover:bg-card hover:text-primary"
        >
          <PlusIcon size={14} /> Add step
        </button>
      </div>

      {!isLast && !stacked && (
        <div
          onMouseDown={startResize}
          className="group flex w-2 shrink-0 cursor-col-resize items-center justify-center"
        >
          <div className="h-8 w-px rounded bg-subtle transition-colors group-hover:bg-default" />
        </div>
      )}
    </>
  )
}
