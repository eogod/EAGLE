import { afterEach, beforeEach, describe, expect, it } from 'bun:test'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { agentApi } from '../../entities/agentApi'
import { AgentPanel } from './AgentPanel'

const proposal = (over = {}) => ({
  public_id: 'p1',
  kind: 'edit',
  status: 'proposed',
  summary: 'add one step',
  operations: [
    {
      op: 'add_step',
      phase: 'Execution',
      step_type: 'bash',
      tool_name: 'shell',
      command: 'id',
    },
  ],
  validation: { errors: [], warnings: [], dangerous_steps: [] },
  expires_at: Math.floor(Date.now() / 1000) + 600,
  base_fingerprint: 'f',
  result: null,
  error: null,
  can_approve: true,
  can_apply: false,
  can_execute: false,
  ...over,
})

const approved = proposal({ status: 'approved', can_approve: false, can_apply: true })
const applied = proposal({ status: 'applied', can_approve: false, can_apply: false })

// the backend persists assistant parts, so a reload after the stream must see them
const persistedPart = (publicId) => ({
  type: 'edit-proposal',
  proposalId: publicId,
  kind: 'edit',
  status: 'proposed',
})

const originals = {}
const API = [
  'listSessions',
  'getProviders',
  'getSession',
  'listProposals',
  'getProposal',
  'approveProposal',
  'applyProposal',
  'rejectProposal',
  'executeProposal',
  'streamMessage',
  'importDraft',
  'cancelRun',
]

let calls
let behavior
let storedMessages

const answer = (name) => behavior[name]

const stub = () => {
  calls = []
  storedMessages = []
  agentApi.listSessions = async () => [{ id: 1, chain_id: 5, title: 'case' }]
  agentApi.getProviders = async () => ({
    providers: [
      { id: 'ollama', label: 'Ollama', available: true, models: ['llama'], default: 'llama' },
    ],
    default: { provider: 'ollama', model: 'llama' },
  })
  agentApi.getSession = async () => ({
    id: 1,
    messages: storedMessages,
    draft: null,
    documents: [],
  })
  agentApi.listProposals = async (sessionId, status) => {
    calls.push(['listProposals', sessionId, status])
    return answer('listProposals') ?? []
  }
  agentApi.getProposal = async (sessionId, publicId) => {
    calls.push(['getProposal', sessionId, publicId])
    return answer('getProposal') ?? proposal()
  }
  for (const action of ['approve', 'apply', 'reject', 'execute']) {
    agentApi[`${action}Proposal`] = async (sessionId, publicId, flag) => {
      calls.push([`${action}Proposal`, sessionId, publicId, flag])
      const handler = answer(`${action}Proposal`)
      if (handler) return handler()
      return action === 'approve' ? approved : applied
    }
  }
  agentApi.streamMessage = async function* (sessionId, body) {
    calls.push(['streamMessage', sessionId, body])
    // the backend persists the parts before the terminal event, not after it
    storedMessages = answer('storedMessages') ?? []
    for (const event of answer('streamMessage') ?? []) yield event
  }
  agentApi.importDraft = async () => ({ steps: [] })
  agentApi.cancelRun = async () => ({})
}

const mount = async () => {
  stub()
  const chainChanges = []
  render(
    <AgentPanel
      chainId={5}
      chainName="op"
      collapsed={false}
      onToggleCollapse={() => {}}
      onChainChanged={() => chainChanges.push(Date.now())}
      timingEnabled={false}
      onToggleTiming={() => {}}
      plannedMinutes={0}
    />,
  )
  await screen.findByText('Agent')
  await screen.findByRole('button', { name: 'ask' })
  return { chainChanges }
}

const callNames = () => calls.map(([name]) => name)
const findCall = (name) => calls.find(([called]) => called === name)

const sendMessage = async (text) => {
  // the send button shares its label with the mode button, so stay outside the form
  const modeButton = screen
    .getAllByRole('button', { name: 'edit' })
    .find((button) => !button.closest('form'))
  fireEvent.click(modeButton)

  const input = await screen.findByPlaceholderText('Describe the change you want proposed…')
  fireEvent.change(input, { target: { value: text } })
  const composer = input.closest('form')
  fireEvent.click(within(composer).getByText('edit'))
}

beforeEach(() => {
  behavior = {}
  for (const name of API) originals[name] = agentApi[name]
})

afterEach(() => {
  cleanup()
  for (const name of API) agentApi[name] = originals[name]
})

describe('agent proposal review', () => {
  it('turns a streamed proposal into a card and applies it only after approval', async () => {
    behavior.streamMessage = [
      { event: 'assistant.delta', data: { text: 'Here is the plan.' } },
      { event: 'edit.proposed', data: proposal() },
      { event: 'run.completed', data: { run_id: 1 } },
    ]
    behavior.storedMessages = [
      {
        id: 2,
        role: 'assistant',
        content: [{ type: 'text', text: 'Here is the plan.' }, persistedPart('p1')],
      },
    ]
    const { chainChanges } = await mount()

    await sendMessage('add the step')

    expect(await screen.findByText('Chain edit')).toBeTruthy()
    expect(screen.getByText('add one step')).toBeTruthy()
    expect(screen.getByText('Add step · Execution · bash')).toBeTruthy()
    expect(screen.getByText('shell · id')).toBeTruthy()
    // the reload after the run restored the card from the persisted part
    expect(findCall('getProposal')).toEqual(['getProposal', 1, 'p1'])
    expect(findCall('streamMessage')[2]).toMatchObject({ content: 'add the step', mode: 'edit' })

    fireEvent.click(screen.getByText('Approve'))

    await waitFor(() => expect(findCall('approveProposal')).toBeTruthy())
    // operations never travel back with the decision
    expect(findCall('approveProposal')).toEqual(['approveProposal', 1, 'p1', false])
    expect(callNames()).not.toContain('applyProposal')

    fireEvent.click(await screen.findByText('Apply'))

    await waitFor(() => expect(findCall('applyProposal')).toBeTruthy())
    expect(findCall('applyProposal')).toEqual(['applyProposal', 1, 'p1', false])

    // the canvas is told to refetch once the chain really changed
    await waitFor(() => expect(chainChanges.length).toBeGreaterThan(0))
    expect(await screen.findByText('applied')).toBeTruthy()
    expect(screen.queryByText('Approve')).toBeNull()
    expect(screen.queryByText('Apply')).toBeNull()
  })

  it('keeps the review open when the chain moved under it', async () => {
    behavior.streamMessage = [
      { event: 'edit.proposed', data: proposal() },
      { event: 'run.completed', data: { run_id: 1 } },
    ]
    behavior.storedMessages = [{ id: 2, role: 'assistant', content: [persistedPart('p1')] }]
    behavior.approveProposal = () => {
      const error = new Error('conflict')
      error.response = {
        status: 409,
        data: { detail: 'chain changed; review the proposal again' },
      }
      throw error
    }
    const { chainChanges } = await mount()

    await sendMessage('add the step')
    fireEvent.click(await screen.findByText('Approve'))

    expect(await screen.findByText(/chain changed/i)).toBeTruthy()
    // the card re-reads the server state instead of guessing
    await waitFor(() => expect(calls.filter(([name]) => name === 'getProposal').length).toBe(2))
    expect(screen.getByText('Approve')).toBeTruthy()
    expect(chainChanges).toHaveLength(0)
  })

  it('restores a pending review after a dropped stream', async () => {
    behavior.listProposals = [proposal({ public_id: 'p2', summary: 'run the step' })]
    const { chainChanges } = await mount()

    expect(await screen.findByText('1 proposal(s) awaiting review')).toBeTruthy()
    expect(await screen.findByText('run the step')).toBeTruthy()
    expect(chainChanges).toHaveLength(0)
  })
})
