import { describe, expect, it } from 'bun:test'
import { appendText, mergeProposals, reduceParts } from './agentParts'

const proposal = (publicId, status = 'proposed') => ({
  public_id: publicId,
  kind: 'edit',
  status,
  summary: 'one step',
})

const textPart = (text) => [{ type: 'text', text }]

describe('reduceParts', () => {
  it('adds an edit-proposal part from the SSE payload', () => {
    const parts = reduceParts([], {
      event: 'edit.proposed',
      data: proposal('p1'),
    })

    expect(parts).toEqual([
      {
        type: 'edit-proposal',
        proposalId: 'p1',
        kind: 'edit',
        status: 'proposed',
        proposal: proposal('p1'),
      },
    ])
  })

  it('accepts both proposal_id and public_id spellings', () => {
    const parts = reduceParts([], {
      event: 'edit.proposed',
      data: { proposal_id: 'p2', public_id: 'ignored', status: 'proposed' },
    })

    expect(parts[0].proposalId).toBe('p2')
  })

  it('ignores a proposal event without an id', () => {
    const before = textPart('a')

    expect(reduceParts(before, { event: 'edit.proposed', data: {} })).toBe(before)
  })

  it('upserts instead of duplicating the same card', () => {
    const first = reduceParts([], { event: 'edit.proposed', data: proposal('p1') })
    const next = reduceParts(first, {
      event: 'edit.proposed',
      data: proposal('p1', 'approved'),
    })

    expect(next).toHaveLength(1)
    expect(next[0].status).toBe('approved')
    expect(next[0].proposal.status).toBe('approved')
  })

  it('closes a tool card on tool.completed and flags errors', () => {
    const started = reduceParts([], {
      event: 'tool.started',
      data: { toolCallId: 't1', toolName: 'edit_chain', args: { kind: 'edit' } },
    })
    const done = reduceParts(started, {
      event: 'tool.completed',
      data: { toolCallId: 't1', status: 'error', result: { proposal_id: 'p1' } },
    })

    expect(done[0].isPreliminary).toBeUndefined()
    expect(done[0].isError).toBe(true)
    expect(done[0].result).toEqual({ proposal_id: 'p1' })
  })

  it('coalesces streamed text and appends errors as text', () => {
    const first = reduceParts([], { event: 'assistant.delta', data: { text: 'Hel' } })
    const second = reduceParts(first, { event: 'assistant.delta', data: { text: 'lo' } })
    const failed = reduceParts(second, { event: 'run.error', data: {} })

    expect(second).toEqual([{ type: 'text', text: 'Hello' }])
    expect(failed[0].text).toContain('analysis failed')
  })
})

describe('appendText', () => {
  it('returns the same array for an empty delta', () => {
    const parts = textPart('x')

    expect(appendText(parts, '')).toBe(parts)
  })
})

describe('mergeProposals', () => {
  it('returns the same messages when nothing is pending', () => {
    const messages = [{ id: '1', role: 'assistant', content: [] }]

    expect(mergeProposals(messages, [])).toBe(messages)
  })

  it('refreshes a card that the dropped stream left behind', () => {
    const messages = [
      {
        id: '1',
        role: 'assistant',
        content: [{ type: 'edit-proposal', proposalId: 'p1', status: 'proposed' }],
      },
    ]

    const merged = mergeProposals(messages, [proposal('p1', 'approved')])

    expect(merged).toHaveLength(1)
    expect(merged[0].content[0].status).toBe('approved')
    expect(merged[0].content[0].proposal.summary).toBe('one step')
  })

  it('appends missing cards to the last assistant message', () => {
    const messages = [{ id: '1', role: 'assistant', content: textPart('hello') }]

    const merged = mergeProposals(messages, [proposal('p9')])

    expect(merged).toHaveLength(1)
    expect(merged[0].content.map((part) => part.type)).toEqual(['text', 'edit-proposal'])
  })

  it('creates a synthetic message when the thread is empty', () => {
    const merged = mergeProposals([], [proposal('p1'), proposal('p2')])

    expect(merged).toHaveLength(1)
    expect(merged[0].id).toBe('pending-proposals')
    expect(merged[0].content.map((part) => part.proposalId)).toEqual(['p1', 'p2'])
  })
})
