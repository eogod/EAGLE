import { api } from '../shared/api/axios'

const BASE = '/agent'
// raw fetch (not axios) so the SSE body can be read incrementally
const FETCH_BASE = '/api/agent'

const data = (promise) => promise.then((response) => response.data)

export const agentApi = {
  listSessions: () => data(api.get(`${BASE}/sessions`)),
  createSession: (payload) => data(api.post(`${BASE}/sessions`, payload)),
  getSession: (sessionId) => data(api.get(`${BASE}/sessions/${sessionId}`)),
  renameSession: (sessionId, title) => data(api.patch(`${BASE}/sessions/${sessionId}`, { title })),
  deleteSession: (sessionId) => api.delete(`${BASE}/sessions/${sessionId}`),

  uploadDocument: (sessionId, file) => {
    const form = new FormData()
    form.append('file', file, file.name)
    return data(api.post(`${BASE}/sessions/${sessionId}/documents`, form))
  },

  getProviders: () => data(api.get(`${BASE}/providers`)),
  cancelRun: (runId) => data(api.post(`${BASE}/runs/${runId}/cancel`)),
  getDraft: (draftId) => data(api.get(`${BASE}/drafts/${draftId}`)),
  importDraft: (draftId, chainName) =>
    data(api.post(`${BASE}/drafts/${draftId}/import`, { chain_name: chainName })),

  async *streamMessage(sessionId, body, signal) {
    const response = await fetch(`${FETCH_BASE}/sessions/${sessionId}/messages`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal,
    })

    if (!response.ok) {
      let detail = `request failed (${response.status})`
      try {
        const payload = await response.json()
        detail = typeof payload.detail === 'string' ? payload.detail : detail
      } catch {
        // body was not JSON, keep the status line
      }
      throw new Error(detail)
    }
    if (!response.body) throw new Error('streaming is not supported by this browser')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    for (;;) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf('\n\n')

      while (boundary !== -1) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const event = parseSseBlock(block)
        if (event) yield event

        boundary = buffer.indexOf('\n\n')
      }
    }
  },
}

function parseSseBlock(block) {
  let event = ''
  const payload = []

  for (const line of block.split('\n')) {
    if (line.startsWith('event: ')) event = line.slice(7)
    else if (line.startsWith('data: ')) payload.push(line.slice(6))
  }
  if (!event) return null

  try {
    return { event, data: payload.length ? JSON.parse(payload.join('\n')) : {} }
  } catch {
    return { event, data: {} }
  }
}

export default agentApi
