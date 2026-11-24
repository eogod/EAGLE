import { afterEach, beforeEach, describe, expect, it } from 'bun:test'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MinifyProvider } from '../../context/MinifyContext'
import { chainApi } from '../../entities/chainApi'
import { stepApi } from '../../entities/stepApi'
import { ChainCanvas } from './ChainCanvas'

const PHASE = 'Reconnaissance'
const chain = { chain_id: 5, chain_name: 'op', current_phase_name: PHASE }
const DEBOUNCE_WAIT = { timeout: 2000 }

const serverStep = (over = {}) => ({
  id: 11,
  chain_id: 5,
  phase: PHASE,
  step_type: 'local',
  tool_name: 'shell',
  command: 'whoami',
  status: 'pending',
  raw_log: '',
  is_draft: false,
  delay_before_secs: 0,
  delay_reason: '',
  delay_enabled: true,
  mythic_payload_uuid: 'u',
  callback_display_id: 0,
  ...over,
})

const originals = {}
const patched = {
  stepApi: [
    'createDraftStep',
    'updateDraftStep',
    'deleteDraftStep',
    'updateStep',
    'deleteStep',
    'runStoredStep',
    'streamStepEvents',
    'updatePhaseTiming',
  ],
  chainApi: ['getChainWithSteps', 'setPhase'],
}

let calls

const stub = (steps) => {
  calls = []
  chainApi.getChainWithSteps = async () => ({ steps })
  chainApi.setPhase = async (chainId, phase) => {
    calls.push(['setPhase', chainId, phase])
  }
  stepApi.createDraftStep = async (chainId, body) => {
    calls.push(['createDraftStep', chainId, body])
    return serverStep({ id: 13, is_draft: true, tool_name: '', command: body.command })
  }
  stepApi.updateDraftStep = async (chainId, stepId, patch) => {
    calls.push(['updateDraftStep', chainId, stepId, patch])
    return {}
  }
  stepApi.deleteDraftStep = async (chainId, stepId) => {
    calls.push(['deleteDraftStep', chainId, stepId])
    return {}
  }
  stepApi.updateStep = async (stepId, patch) => {
    calls.push(['updateStep', stepId, patch])
    return {}
  }
  stepApi.deleteStep = async (stepId) => {
    calls.push(['deleteStep', stepId])
  }
  stepApi.runStoredStep = async (stepId) => {
    calls.push(['runStoredStep', stepId])
    return { step_id: 99, status: 'submitted' }
  }
  // the live stream is covered by its own tests; here it only must not hang
  stepApi.streamStepEvents = async () => {}
  stepApi.updatePhaseTiming = async () => ({})
}

const mount = async (steps) => {
  stub(steps)
  render(
    <MinifyProvider>
      <ChainCanvas currentChain={chain} />
    </MinifyProvider>,
  )
  await waitFor(() => expect(screen.getByDisplayValue(steps[0].command)).toBeTruthy())
}

const callNames = () => calls.map(([name]) => name)
const findCall = (name) => calls.find(([called]) => called === name)

beforeEach(() => {
  for (const [module, names] of Object.entries(patched)) {
    const target = module === 'stepApi' ? stepApi : chainApi
    for (const name of names) {
      originals[`${module}:${name}`] = target[name]
    }
  }
  globalThis.window.confirm = () => true
})

afterEach(() => {
  cleanup()
  for (const [module, names] of Object.entries(patched)) {
    const target = module === 'stepApi' ? stepApi : chainApi
    for (const name of names) target[name] = originals[`${module}:${name}`]
  }
})

describe('canvas draft lifecycle', () => {
  it('creates the draft on the first command and edits it as a draft', async () => {
    await mount([serverStep()])

    fireEvent.click(screen.getByText('Add step'))
    const empty = screen.getAllByPlaceholderText('Command to run').find((node) => node.value === '')
    fireEvent.change(empty, { target: { value: 'id' } })

    await waitFor(() => expect(findCall('createDraftStep')).toBeTruthy())
    expect(findCall('createDraftStep')).toEqual([
      'createDraftStep',
      5,
      {
        phase: PHASE,
        step_type: 'local',
        tool_name: '',
        command: 'id',
        delay_before_secs: 0,
      },
    ])

    // the card now carries the server id, so edits go to the draft route
    await waitFor(() => expect(screen.getByDisplayValue('id')).toBeTruthy())
    fireEvent.change(screen.getByDisplayValue('id'), { target: { value: 'id; uname' } })

    await waitFor(() => expect(findCall('updateDraftStep')).toBeTruthy(), DEBOUNCE_WAIT)
    expect(findCall('updateDraftStep')).toEqual([
      'updateDraftStep',
      5,
      13,
      { command: 'id; uname' },
    ])
    expect(callNames()).not.toContain('updateStep')
  })

  it('debounces typing into one patch instead of one per keystroke', async () => {
    await mount([serverStep({ is_draft: true, id: 12, command: 'ls' })])

    const draft = screen.getByDisplayValue('ls')
    fireEvent.change(draft, { target: { value: 'ls -' } })
    fireEvent.change(draft, { target: { value: 'ls -la' } })

    await waitFor(() => expect(findCall('updateDraftStep')).toBeTruthy(), DEBOUNCE_WAIT)
    await new Promise((resolve) => setTimeout(resolve, 400))

    const patches = calls.filter(([name]) => name === 'updateDraftStep')
    expect(patches).toHaveLength(1)
    expect(patches[0]).toEqual(['updateDraftStep', 5, 12, { command: 'ls -la' }])
  })

  it('keeps saved steps on the real-step routes', async () => {
    await mount([serverStep()])

    fireEvent.change(screen.getByDisplayValue('whoami'), {
      target: { value: 'whoami -a' },
    })

    await waitFor(() => expect(findCall('updateStep')).toBeTruthy(), DEBOUNCE_WAIT)
    expect(findCall('updateStep')).toEqual(['updateStep', 11, { command: 'whoami -a' }])
    expect(callNames()).not.toContain('updateDraftStep')
  })

  it('deletes each kind of card through its own route', async () => {
    await mount([
      serverStep(),
      serverStep({ id: 12, is_draft: true, command: 'ls', tool_name: '' }),
    ])

    const draftCard = screen.getByDisplayValue('ls').closest('.rounded-lg')
    fireEvent.click(within(draftCard).getByText('Delete'))

    await waitFor(() => expect(findCall('deleteDraftStep')).toBeTruthy())
    expect(findCall('deleteDraftStep')).toEqual(['deleteDraftStep', 5, 12])

    const realCard = screen.getByDisplayValue('whoami').closest('.rounded-lg')
    fireEvent.click(within(realCard).getByText('Delete'))

    await waitFor(() => expect(findCall('deleteStep')).toBeTruthy())
    expect(findCall('deleteStep')).toEqual(['deleteStep', 11])
  })

  it('saves a queued edit before running the step', async () => {
    await mount([serverStep()])

    fireEvent.change(screen.getByDisplayValue('whoami'), {
      target: { value: 'uname -a' },
    })
    const card = screen.getByDisplayValue('uname -a').closest('.rounded-lg')
    fireEvent.click(within(card).getByText('Run'))

    await waitFor(() => expect(findCall('runStoredStep')).toBeTruthy(), DEBOUNCE_WAIT)

    // the server reads the command from the db, so the patch must be first
    expect(callNames().indexOf('updateStep')).toBeLessThan(callNames().indexOf('runStoredStep'))
    expect(findCall('updateStep')).toEqual(['updateStep', 11, { command: 'uname -a' }])
    expect(findCall('runStoredStep')).toEqual(['runStoredStep', 11])
  })
})
