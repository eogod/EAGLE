import { afterEach, beforeEach, describe, expect, it } from 'bun:test'
import { api } from './axios'

// the interceptor redirects by assigning window.location.href
globalThis.window = { location: { href: '/' } }

let seen
let behavior

const response = (config, status = 200, data = {}) => ({
  data,
  status,
  statusText: String(status),
  headers: {},
  config,
})

beforeEach(() => {
  seen = []
  globalThis.window.location.href = '/'
  api.defaults.adapter = async (config) => {
    const path = `${(config.method || 'get').toUpperCase()} ${config.url}`
    seen.push(path)
    const [status, data] = behavior(path, config) ?? [200, {}]
    if (status >= 400) {
      const error = new Error(`status ${status}`)
      error.config = config
      error.response = response(config, status, data)
      throw error
    }
    return response(config, status, data)
  }
})

afterEach(() => {
  delete api.defaults.adapter
})

describe('silent access refresh', () => {
  it('refreshes once and replays the original request', async () => {
    behavior = (path, config) => {
      if (path === 'GET /users/me') {
        return config._retried ? [200, { ok: true }] : [401, {}]
      }
      if (path === 'POST /auth/refresh-token') return [200, {}]
    }

    const result = await api.get('/users/me')

    expect(result.data).toEqual({ ok: true })
    expect(seen).toEqual(['GET /users/me', 'POST /auth/refresh-token', 'GET /users/me'])
  })

  it('shares one refresh between parallel 401s', async () => {
    behavior = (path, config) => {
      if (path === 'POST /auth/refresh-token') return [200, {}]
      return config._retried ? [200, {}] : [401, {}]
    }

    await Promise.all([api.get('/one'), api.get('/two')])

    // two rotations would look like a stolen token to the server
    expect(seen.filter((path) => path === 'POST /auth/refresh-token')).toHaveLength(1)
  })

  it('sends the user to login when the refresh itself fails', async () => {
    behavior = (path) => (path === 'POST /auth/refresh-token' ? [401, {}] : [401, {}])

    await expect(api.get('/users/me')).rejects.toMatchObject({
      response: { status: 401 },
    })
    expect(globalThis.window.location.href).toBe('/login')
  })

  it('never retries the auth endpoints', async () => {
    behavior = () => [401, {}]

    await expect(api.post('/auth/access-token')).rejects.toMatchObject({
      response: { status: 401 },
    })
    expect(seen).toEqual(['POST /auth/access-token'])
    expect(globalThis.window.location.href).toBe('/login')
  })

  it('leaves non-auth errors alone', async () => {
    behavior = () => [409, { detail: 'chain changed' }]

    await expect(api.post('/agent/sessions/1/proposals/p/apply')).rejects.toMatchObject({
      response: { status: 409 },
    })
    expect(globalThis.window.location.href).toBe('/')
  })
})
