import { STEP_TYPES } from '../../entities/types'

// the api rejects unknown keys on a step patch, so the card's own fields
// (status, raw_log, _tempId) must never leave the browser
export const API_FIELDS = new Set([
  'phase',
  'step_type',
  'tool_name',
  'command',
  'delay_before_secs',
  'delay_reason',
  'delay_enabled',
])

// debounced fields: everything else can be saved on the spot
export const TEXT_FIELDS = new Set(['command', 'tool_name', 'delay_reason'])

const isAgentStep = (step) => step.step_type === STEP_TYPES.AGENT

export const cardCommand = (step) =>
  ((isAgentStep(step) ? step.command_params : step.command) ?? '').toString()

export const cardTool = (step) => (isAgentStep(step) ? step.tool : step.tool_name) ?? ''

export const mapServerStep = (step, chainName) => {
  const agent = step.step_type === STEP_TYPES.AGENT
  return {
    id: step.id,
    is_draft: step.is_draft ?? false,
    phase: step.phase,
    chain_name: chainName,
    step_type: step.step_type ?? STEP_TYPES.LOCAL,
    callback_display_id: step.callback_display_id ?? 0,
    tool_name: step.tool_name,
    command: agent ? '' : step.command,
    command_params: agent ? step.command : '',
    tool: agent ? step.tool_name : 'shell',
    status: step.status,
    raw_log: step.raw_log,
    delay_before_secs: step.delay_before_secs ?? 0,
    delay_reason: step.delay_reason ?? '',
    delay_enabled: step.delay_enabled ?? true,
  }
}

// the card names agent fields differently; the API keeps both in command/tool_name
export const toApiPatch = (patch) => {
  const next = { ...patch }
  if (next.command_params !== undefined) {
    next.command = next.command_params
    delete next.command_params
  }
  if (next.tool !== undefined) {
    next.tool_name = next.tool
    delete next.tool
  }
  return Object.fromEntries(Object.entries(next).filter(([field]) => API_FIELDS.has(field)))
}

export const sameStep = (candidate, step) =>
  // a local card has no db id yet, so it is matched by its temp key
  step.id != null ? candidate.id === step.id : candidate._tempId === step._tempId
