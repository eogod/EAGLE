import { describe, expect, it } from 'bun:test'
import {
  SETTLED,
  clock,
  describeOperation,
  errorText,
  millisLeft,
  proposalView,
} from './proposalView'

const NOW = 1_700_000_000_000

const proposal = (overrides = {}) => ({
  public_id: 'p1',
  kind: 'edit',
  status: 'proposed',
  validation: { errors: [], warnings: [], dangerous_steps: [] },
  expires_at: NOW / 1000 + 60,
  can_approve: true,
  can_apply: false,
  can_execute: false,
  ...overrides,
})

describe('millisLeft', () => {
  it('reads unix seconds as seconds', () => {
    expect(millisLeft(NOW / 1000 + 60, NOW)).toBe(60_000)
  })

  it('keeps millisecond stamps as milliseconds', () => {
    expect(millisLeft(NOW + 60_000, NOW)).toBe(60_000)
  })

  it('treats missing or nonsense expiry as no deadline', () => {
    for (const value of [null, undefined, '', 0, -5, 'soon']) {
      expect(millisLeft(value, NOW)).toBeNull()
    }
  })
})

describe('clock', () => {
  it('formats a countdown and never goes negative', () => {
    expect(clock(0)).toBe('0:00')
    expect(clock(65_000)).toBe('1:05')
    expect(clock(-9_000)).toBe('0:00')
  })
})

describe('errorText', () => {
  const boom = (status, detail) => ({ response: { status, data: { detail } } })

  it('explains the three review failures', () => {
    expect(errorText(boom(409))).toContain('chain changed')
    expect(errorText(boom(410))).toContain('expired')
    expect(errorText(boom(404))).toContain('no longer exists')
  })

  it('prefers the server detail when present', () => {
    expect(errorText(boom(409, 'step changed; review again'))).toBe('step changed; review again')
    expect(errorText(boom(422, { message: 'invalid plan' }))).toBe('invalid plan')
  })

  it('falls back to the transport message', () => {
    expect(errorText(new Error('Network Error'))).toBe('Network Error')
    expect(errorText({})).toBe('Request failed')
  })
})

describe('describeOperation', () => {
  it('describes each operation the backend can emit', () => {
    expect(
      describeOperation({
        op: 'add_step',
        phase: 'Execution',
        step_type: 'bash',
        tool_name: 'shell',
        command: 'id',
      }),
    ).toEqual({ title: 'Add step · Execution · bash', lines: ['shell · id'] })

    expect(describeOperation({ op: 'remove_step', step_id: 3, reason: 'noise' })).toEqual({
      title: 'Remove step #3',
      lines: ['noise'],
    })

    expect(describeOperation({ op: 'set_phase', phase: 'Execution' })).toEqual({
      title: 'Set phase',
      lines: ['current → Execution'],
    })

    expect(describeOperation({ op: 'run_step', step_id: 4 })).toEqual({
      title: 'Run step #4',
      lines: [],
    })
  })

  it('does not crash on an unknown operation', () => {
    expect(describeOperation({ op: 'mystery' }).title).toBe('mystery')
    expect(describeOperation({}).title).toBe('operation')
  })
})

describe('proposalView', () => {
  it('offers only the actions the server allows', () => {
    const view = proposalView(proposal({ can_apply: true }))

    expect(view.canApprove).toBe(true)
    expect(view.canApply).toBe(true)
    expect(view.canExecute).toBe(false)
    expect(view.blocked).toBe(false)
  })

  it('keeps the button but blocks it until dangerous steps are acknowledged', () => {
    const dangerous = proposal({ validation: { errors: [], warnings: [], dangerous_steps: [2] } })

    const pending = proposalView(dangerous)
    expect(pending.needsAck).toBe(true)
    expect(pending.canApprove).toBe(true)
    expect(pending.blocked).toBe(true)

    expect(proposalView(dangerous, { acknowledged: true }).blocked).toBe(false)
  })

  it('blocks on the client-side countdown even before the server says expired', () => {
    const view = proposalView(proposal(), { left: 0 })

    expect(view.expired).toBe(true)
    expect(view.blocked).toBe(true)
  })

  it('offers nothing once the card settles', () => {
    for (const status of SETTLED) {
      const view = proposalView(proposal({ status, can_approve: true, can_apply: true }))
      expect(view.unsettled).toBe(false)
      expect(view.canApprove).toBe(false)
      expect(view.canApply).toBe(false)
      expect(view.canReject).toBe(false)
    }
  })

  it('treats every open status as reviewable', () => {
    for (const status of ['proposed', 'approved', 'applying', 'executing']) {
      expect(proposalView(proposal({ status })).unsettled).toBe(true)
    }
  })

  it('never lets a second request race an in-flight one', () => {
    const view = proposalView(proposal(), { busy: 'apply' })

    expect(view.blocked).toBe(true)
    expect(view.busy).toBe(true)
  })

  it('lets a missing card (404) offer nothing', () => {
    const view = proposalView(proposal(), { status: 'missing' })

    expect(view.unsettled).toBe(false)
    expect(view.canApprove).toBe(false)
  })
})
