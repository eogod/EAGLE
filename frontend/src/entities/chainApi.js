import { api } from '../shared/api/axios'

const BASE = '/cmd'
const FETCH_BASE = '/api/cmd'

export const chainApi = {
  list: () => api.get(`${BASE}/chains`).then((res) => res.data),

  create: (chainName) =>
    api.post(`${BASE}/new-chain`, { chain_name: chainName }).then((res) => res.data),
  delete: (chainId) => api.delete(`${BASE}/chains/${chainId}`).then((res) => res.data),

  getChainInfo: (chainId) => api.get(`${BASE}/chain-phase/${chainId}`).then((res) => res.data),

  getChainWithSteps: (chainId) => api.get(`${BASE}/chain/${chainId}`).then((res) => res.data),

  nextPhase: (chainId) => api.post(`${BASE}/next-phase/${chainId}`).then((res) => res.data),
  setPhase: (chainId, phaseName) =>
    api
      .post(`${BASE}/set-phase/${chainId}`, null, {
        params: { phase_name: phaseName },
      })
      .then((res) => res.data),
  // can depend on current UCKC phase
  rejectLastStep: (chainName) => api.post(`${BASE}/reject-s/${chainName}`).then((res) => res.data),
  // stop chain execution , works after 30-120 sec cuz all commands via Mythic
  cancelChain: (chainId, chainName) =>
    api
      .post(`${BASE}/cancel-chain/${chainId}`, null, {
        params: { chain_name: chainName },
      })
      .then((res) => res.data),

  // run-chain streams one JSON line per executed step; consume it so the UI
  // can report what actually happened instead of a blind "started"
  runChain: async (chainId, { timing = false, onEvent } = {}) => {
    const response = await fetch(
      `${FETCH_BASE}/run-chain/${chainId}?zero_display_id=0&timing=${timing}`,
      {
        method: 'POST',
        credentials: 'include',
        headers: { Accept: 'application/json' },
      },
    )
    if (!response.ok) {
      let detail = `run failed (${response.status})`
      try {
        detail = (await response.json()).detail || detail
      } catch {
        // keep the status line
      }
      throw new Error(detail)
    }
    if (!response.body) return { steps: [], errors: [] }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    const steps = []
    const errors = []
    let buffer = ''

    for (;;) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      let newline = buffer.indexOf('\n')

      while (newline !== -1) {
        const line = buffer.slice(0, newline).trim()
        buffer = buffer.slice(newline + 1)
        if (line) {
          try {
            const payload = JSON.parse(line)
            if (payload.AttackStep) steps.push(payload.AttackStep)
            if (payload.error) errors.push(payload.error)
            if (payload.type) onEvent?.(payload)
          } catch {
            // ignore a malformed line, keep reading
          }
        }
        newline = buffer.indexOf('\n')
      }
    }

    return { steps, errors }
  },

  getCurrentUser: () => api.get('/users/me').then((res) => res.data),
}
