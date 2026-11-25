// nothing can be reviewed once a proposal reaches these states
export const SETTLED = [
  'applied',
  'rejected',
  'executed',
  'expired',
  'invalid',
  'failed',
  'missing',
]

export const KIND_LABELS = { edit: 'Chain edit', run: 'Run request' }

export const STATUS_STYLES = {
  proposed: 'text-warning',
  approved: 'text-accent',
  applied: 'text-success',
  executed: 'text-success',
  rejected: 'text-muted',
  expired: 'text-danger',
  invalid: 'text-danger',
  failed: 'text-danger',
  missing: 'text-danger',
}

// expires_at arrives as unix seconds; keep millisecond stamps working too
export const millisLeft = (expiresAt, now) => {
  const value = Number(expiresAt)
  if (!Number.isFinite(value) || value <= 0) return null
  return (value > 1e12 ? value : value * 1000) - now
}

export const clock = (ms) => {
  const total = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

export const describeOperation = (operation) => {
  if (operation.op === 'add_step') {
    return {
      title: `Add step · ${operation.phase ?? '?'} · ${operation.step_type ?? '?'}`,
      lines: [[operation.tool_name, operation.command].filter(Boolean).join(' · ')],
    }
  }
  if (operation.op === 'remove_step') {
    return { title: `Remove step #${operation.step_id}`, lines: [operation.reason].filter(Boolean) }
  }
  if (operation.op === 'set_phase') {
    return {
      title: 'Set phase',
      lines: [`${operation.from_phase || 'current'} → ${operation.phase ?? '?'}`],
    }
  }
  if (operation.op === 'run_step') {
    return { title: `Run step #${operation.step_id}`, lines: [] }
  }
  return { title: operation.op || 'operation', lines: [] }
}

export const errorText = (err) => {
  const status = err?.response?.status
  const detail = err?.response?.data?.detail
  const message = typeof detail === 'string' ? detail : detail?.message

  if (status === 409) {
    return (
      message || 'The chain changed since this was proposed. Ask the agent for a fresh proposal.'
    )
  }
  if (status === 410) return message || 'This proposal expired. Ask the agent to propose it again.'
  if (status === 404) return 'This proposal no longer exists.'
  return message || err.message || 'Request failed'
}

/** which review actions the card may offer right now, and why they are blocked */
export const proposalView = (
  proposal,
  { left = null, acknowledged = false, busy = false, status } = {},
) => {
  const current = status ?? proposal?.status
  const dangerous = proposal?.validation?.dangerous_steps ?? []
  const expired = (left != null && left <= 0) || current === 'expired'
  const unsettled = !SETTLED.includes(current)
  const needsAck = dangerous.length > 0 && !acknowledged

  return {
    status: current,
    unsettled,
    expired,
    needsAck,
    busy: Boolean(busy),
    blocked: Boolean(busy) || expired || needsAck,
    // can_* comes from the server; a visible but disabled button explains why
    canApprove: unsettled && Boolean(proposal?.can_approve),
    canApply: unsettled && Boolean(proposal?.can_apply),
    canExecute: unsettled && Boolean(proposal?.can_execute),
    // a rejection stays possible while danger is unacknowledged
    canReject: unsettled,
  }
}
