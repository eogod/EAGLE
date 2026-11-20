/**
 * @typedef {'pending'|'running'|'success'|'error'|'rejected'} StepStatus
 * @typedef {'local'|'agent'} StepType
 */

/**
 * @typedef {Object} Step
 * @property {number|null} id
 * @property {number} chain_id
 * @property {string} chain_name
 * @property {string} phase
 * @property {string} tool_name
 * @property {StepType} step_type
 * @property {number} callback_display_id
 * @property {string} command
 * @property {string|null} command_params
 * @property {string|null} tool
 * @property {string} mythic_payload_uuid
 * @property {StepStatus} status
 * @property {string} raw_log
 */

/**
 * @typedef {Object} Chain
 * @property {number} chain_id
 * @property {string} chain_name
 * @property {string} current_phase_name
 */

export const STEP_STATUS = {
  PENDING: 'pending',
  RUNNING: 'running',
  SUCCESS: 'success',
  ERROR: 'error',
  REJECTED: 'rejected',
}

export const STEP_TYPES = {
  LOCAL: 'local',
  AGENT: 'agent',
  BASH: 'bash',
}
