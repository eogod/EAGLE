import { afterEach, beforeEach, describe, expect, it } from 'bun:test'
import { api } from '../shared/api/axios'
import { stepApi } from './stepApi'
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

describe('draft steps', () => {
  it('creates a draft on the draft route', async () => {
    const body = {
      phase: 'Execution',
      step_type: 'bash',
      tool_name: 'shell',
      command: 'id',
      delay_before_secs: 0,
    }
    await stepApi.createDraftStep(5, body)

    expect(lastCall()).toMatchObject({ method: 'post', url: '/cmd/chain/5/draft-steps', body })
    expectContractEndpoint('post', `/api${lastCall().url}`)
    expect(Object.keys(lastCall().body)).toEqual(requestFields('DraftStepCreate'))
  })

  it('patches and deletes a draft under its chain', async () => {
    await stepApi.updateDraftStep(5, 9, { command: 'id' })
    expect(lastCall()).toMatchObject({
      method: 'patch',
      url: '/cmd/chain/5/draft-steps/9',
      body: { command: 'id' },
    })
    expectContractEndpoint('patch', `/api${lastCall().url}`)

    await stepApi.deleteDraftStep(5, 9)
    expect(lastCall()).toMatchObject({ method: 'delete', url: '/cmd/chain/5/draft-steps/9' })
    expectContractEndpoint('delete', `/api${lastCall().url}`)
  })
})

describe('saved steps', () => {
  it('runs a stored step without sending the command', async () => {
    await stepApi.runStoredStep(9)

    expect(lastCall()).toMatchObject({
      method: 'post',
      url: '/cmd/steps/9/run',
      body: { expected_step_fingerprint: null },
    })
    expectContractEndpoint('post', `/api${lastCall().url}`)
    expect(lastCall().body.command).toBeUndefined()
  })

  it('patches and deletes only on the real-step routes', async () => {
    await stepApi.updateStep(9, { command: 'id' })
    expect(lastCall()).toMatchObject({ method: 'patch', url: '/cmd/step/9' })
    expectContractEndpoint('patch', `/api${lastCall().url}`)

    await stepApi.deleteStep(9)
    expect(lastCall()).toMatchObject({ method: 'delete', url: '/cmd/step/9' })
    expectContractEndpoint('delete', `/api${lastCall().url}`)
  })

  it('reads one step snapshot', async () => {
    await stepApi.getStep(9)

    expect(lastCall()).toMatchObject({ method: 'get', url: '/cmd/step/9' })
    expectContractEndpoint('get', `/api${lastCall().url}`)
  })
})

describe('phase timing', () => {
  it('sends phase and enabled as query params', async () => {
    await stepApi.updatePhaseTiming(5, 'Execution', false)

    expect(lastCall()).toMatchObject({
      method: 'patch',
      url: '/cmd/chain/5/delay',
      config: { params: { phase: 'Execution', enabled: false } },
    })
    expectContractEndpoint('patch', `/api${lastCall().url}`)
  })
})
