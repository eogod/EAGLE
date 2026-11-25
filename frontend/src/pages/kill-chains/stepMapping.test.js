import { describe, expect, it } from 'bun:test'
import {
  TEXT_FIELDS,
  cardCommand,
  cardTool,
  mapServerStep,
  sameStep,
  toApiPatch,
} from './stepMapping'
import { requestFields } from '../../test/contract'

const localStep = {
  id: 1,
  phase: 'Execution',
  step_type: 'local',
  tool_name: 'shell',
  command: 'id',
  status: 'pending',
  raw_log: '',
}

describe('mapServerStep', () => {
  it('keeps a local step in command/tool_name', () => {
    const card = mapServerStep(localStep, 'chain-a')

    expect(card).toMatchObject({
      id: 1,
      is_draft: false,
      command: 'id',
      command_params: '',
      tool: 'shell',
      delay_enabled: true,
      chain_name: 'chain-a',
    })
  })

  it('moves an agent step into the card-only fields', () => {
    const card = mapServerStep(
      { ...localStep, step_type: 'agent', tool_name: 'psexec', command: 'whoami' },
      'chain-a',
    )

    expect(card.command).toBe('')
    expect(card.command_params).toBe('whoami')
    expect(card.tool).toBe('psexec')
  })

  it('reads the draft flag the server sends back', () => {
    expect(mapServerStep({ ...localStep, is_draft: true }, 'chain-a').is_draft).toBe(true)
  })
})

describe('toApiPatch', () => {
  it('drops fields the server rejects', () => {
    const patch = toApiPatch({
      status: 'success',
      raw_log: 'output',
      callback_display_id: 3,
      _tempId: 9,
      chain_name: 'x',
    })

    expect(patch).toEqual({})
  })

  it('keeps only the contracted step fields', () => {
    const patch = toApiPatch({
      command: 'id',
      phase: 'Execution',
      step_type: 'bash',
      delay_before_secs: 30,
      delay_reason: 'wait',
      delay_enabled: false,
    })

    expect(Object.keys(patch).sort()).toEqual(
      [
        'command',
        'delay_before_secs',
        'delay_enabled',
        'delay_reason',
        'phase',
        'step_type',
      ].sort(),
    )
    expect(requestFields('StepUpdate')).toEqual(expect.arrayContaining(Object.keys(patch)))
  })

  it('translates the agent card names into api columns', () => {
    expect(toApiPatch({ command: 'old', command_params: 'new', tool: 'wmiexec' })).toEqual({
      command: 'new',
      tool_name: 'wmiexec',
    })
  })

  it('leaves nothing on the wire for an untouched field', () => {
    expect(JSON.stringify(toApiPatch({ command: undefined }))).toBe('{}')
  })
})

describe('card helpers', () => {
  it('reads the right command and tool per step type', () => {
    const agent = { step_type: 'agent', command: 'a', command_params: 'b', tool: 't' }

    expect(cardCommand({ step_type: 'local', command: 'id' })).toBe('id')
    expect(cardCommand(agent)).toBe('b')
    expect(cardTool({ step_type: 'local', tool_name: 'shell' })).toBe('shell')
    expect(cardTool(agent)).toBe('t')
  })

  it('debounces exactly the free-text fields', () => {
    expect([...TEXT_FIELDS].sort()).toEqual(['command', 'delay_reason', 'tool_name'])
  })
})

describe('sameStep', () => {
  it('matches by server id once the row exists', () => {
    expect(sameStep({ id: 1 }, { id: 1 })).toBe(true)
    expect(sameStep({ id: 2 }, { id: 1 })).toBe(false)
  })

  it('matches local cards by temp id until the server answers', () => {
    expect(sameStep({ id: null, _tempId: 4 }, { id: null, _tempId: 4 })).toBe(true)
    expect(sameStep({ id: null, _tempId: 5 }, { id: null, _tempId: 4 })).toBe(false)
  })
})
