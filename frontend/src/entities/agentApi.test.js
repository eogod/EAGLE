import { afterEach, beforeEach, describe, expect, it, mock } from 'bun:test'
import { api } from '../shared/api/axios'
import { agentApi } from './agentApi'
import { expectContractEndpoint, requestFields } from '../test/contract'

const calls = []

const stub = (method) => (url, bodyOrConfig, config) => {
  const hasBody = method === 'post' || method === 'patch'
  calls.push({
    method,
    url,
    body: hasBody ? bodyOrConfig : undefined,
    config: hasBody ? config : bodyOrConfig,
  })
  return Promise.resolve({ data: { ok: true } })
}

const originals = {}

beforeEach(() => {
  calls.length = 0
  for (const method of ['get', 'post', 'patch', 'delete']) {
    originals[method] = api[method]
    api[method] = stub(method)
  }
})

afterEach(() => {
  for (const method of ['get', 'post', 'patch', 'delete']) api[method] = originals[method]
})

const lastCall = () => calls[calls.length - 1]

const sseResponse = (text, { ok = true, status = 200, detail } = {}) => {
  // enqueue odd-sized byte slices so multi-byte characters and lines are split
  const bytes = new TextEncoder().encode(text)
  return {
    ok,
    status,
    body: new ReadableStream({
      start(controller) {
        for (let offset = 0; offset < bytes.length; offset += 7) {
          controller.enqueue(bytes.slice(offset, offset + 7))
        }
        controller.close()
      },
    }),
    json: async () => ({ detail }),
  }
}

const readAll = async (iterator) => {
  const events = []
  for await (const event of iterator) events.push(event)
  return events
}

describe('proposal review calls', () => {
  it('lists pending proposals with the status filter', async () => {
    await agentApi.listProposals(7)

    expect(lastCall()).toMatchObject({
      method: 'get',
      url: '/agent/sessions/7/proposals',
      config: { params: { status: 'proposed,approved' } },
    })
    expectContractEndpoint('get', `/api${lastCall().url}`)
  })

  it('fetches one proposal by public id', async () => {
    await agentApi.getProposal(7, 'pub-1')

    expect(lastCall()).toMatchObject({
      method: 'get',
      url: '/agent/sessions/7/proposals/pub-1',
    })
    expectContractEndpoint('get', `/api${lastCall().url}`)
  })

  it('sends only the ack flag on approve, apply and execute', async () => {
    for (const [name, action] of [
      ['approve', agentApi.approveProposal],
      ['apply', agentApi.applyProposal],
      ['execute', agentApi.executeProposal],
    ]) {
      calls.length = 0
      await action(7, 'pub-1', true)

      expect(lastCall()).toMatchObject({
        method: 'post',
        url: `/agent/sessions/7/proposals/pub-1/${name}`,
        body: { acknowledge_dangerous: true },
      })
      expectContractEndpoint('post', `/api${lastCall().url}`)
      expect(Object.keys(lastCall().body)).toEqual(requestFields('ProposalApproveIn'))
      // the browser never re-sends operations; the server owns them
      expect(lastCall().body.operations).toBeUndefined()
    }
  })

  it('rejects a proposal with the operator reason', async () => {
    await agentApi.rejectProposal(7, 'pub-1', 'not now')

    expect(lastCall()).toMatchObject({
      method: 'post',
      url: '/agent/sessions/7/proposals/pub-1/reject',
      body: { reason: 'not now' },
    })
    expectContractEndpoint('post', `/api${lastCall().url}`)
  })
})

describe('agent message stream', () => {
  it('parses events whose bytes are split at arbitrary boundaries', async () => {
    const text =
      'event: edit.proposed\n' +
      'data: {"public_id":"p1","status":"proposed","summary":"привет"}\n\n' +
      'event: assistant.delta\n' +
      'data: {"text":"hi"}\n\n'
    globalThis.fetch = mock(async () => sseResponse(text))

    const events = await readAll(agentApi.streamMessage(3, { content: 'go' }))

    expect(events.map((event) => event.event)).toEqual(['edit.proposed', 'assistant.delta'])
    expect(events[0].data.public_id).toBe('p1')
    expect(events[0].data.summary).toBe('привет')
    expect(events[1].data.text).toBe('hi')
  })

  it('posts to the contracted path with the contracted body', async () => {
    globalThis.fetch = mock(async () => sseResponse(''))
    const body = {
      content: 'go',
      document_id: 4,
      provider: 'ollama',
      model: 'llama',
      mode: 'edit',
    }

    await readAll(agentApi.streamMessage(3, body))

    const [url, init] = globalThis.fetch.mock.calls[0]
    expect(url).toBe('/api/agent/sessions/3/messages')
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('include')
    expectContractEndpoint('post', url)
    expect(Object.keys(JSON.parse(init.body)).sort()).toEqual(
      [...requestFields('AgentMessageIn')].sort(),
    )
  })

  it('surfaces the server detail instead of a bare status', async () => {
    globalThis.fetch = mock(async () => sseResponse('', { ok: false, status: 422, detail: 'bad' }))

    await expect(readAll(agentApi.streamMessage(3, { content: 'go' }))).rejects.toThrow('bad')
  })

  it('refuses a response without a readable body', async () => {
    globalThis.fetch = mock(async () => ({ ok: true, status: 200, body: null }))

    await expect(readAll(agentApi.streamMessage(3, { content: 'go' }))).rejects.toThrow(
      'streaming is not supported',
    )
  })
})
