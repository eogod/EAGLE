import { api } from '../shared/api/axios'

const BASE = '/cmd'
// raw fetch bypasses the axios baseURL, so the api prefix must be explicit
const FETCH_BASE = '/api/cmd'

export const stepApi = {
  runLocalCommand: ({ chain_name, callback_display_id, command, is_analyze_llm, step_id }) =>
    api.post(`${BASE}/run-command`, {
      chain_name,
      callback_display_id,
      command,
      is_analyze_llm,
      step_id: step_id ?? null,
    }),

  runBash: ({ chain_name, command, timeout, step_id }) =>
    // direct bash on host
    api.post(`${BASE}/run-bash`, { chain_name, command, timeout, step_id: step_id ?? null }),

  runAgentCommand: ({
    chain_name,
    callback_display_id,
    command_params,
    tool,
    is_analyze_llm,
    step_id,
  }) =>
    api.post(`${BASE}/run-agent-command`, {
      chain_name,
      callback_display_id,
      command_params: command_params ?? '',
      // TODO: fix the workaround in proc.py -> process_approved_cmd -> tool_name like local_impacket-wmiexec
      tool: tool ?? 'shell',
      is_analyze_llm,
      step_id: step_id ?? null,
    }),

  getStep: (stepId) => api.get(`${BASE}/step/${stepId}`).then((res) => res.data),

  analyzeStep: (stepId) => api.post(`${BASE}/step/${stepId}/analyze`).then((res) => res.data),

  // live ndjson events of step & tasks, cookies ride along
  streamStepEvents: (stepId, onEvent, signal) =>
    fetch(`${FETCH_BASE}/step/${stepId}/events`, {
      signal,
      credentials: 'include',
    }).then(async (resp) => {
      if (!resp.ok || !resp.body) throw new Error(`step events status ${resp.status}`)

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''

      for (;;) {
        const { done, value } = await reader.read()
        if (done) break

        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop()

        lines.forEach((line) => {
          if (!line.trim()) return
          try {
            onEvent(JSON.parse(line))
          } catch {
            // partial or broken row, skip it
          }
        })
      }

      if (buf.trim()) {
        try {
          onEvent(JSON.parse(buf))
        } catch {
          // leftover without newline
        }
      }

      return resp
    }),

  deleteStep: (stepId) => api.delete(`${BASE}/step/${stepId}`).then((res) => res.data),

  updateStep: (stepId, patch) => api.patch(`${BASE}/step/${stepId}`, patch).then((res) => res.data),

  createDraftStep: (chainId, body) =>
    api.post(`${BASE}/chain/${chainId}/draft-steps`, body).then((res) => res.data),

  updateDraftStep: (chainId, stepId, patch) =>
    api.patch(`${BASE}/chain/${chainId}/draft-steps/${stepId}`, patch).then((res) => res.data),

  deleteDraftStep: (chainId, stepId) =>
    api.delete(`${BASE}/chain/${chainId}/draft-steps/${stepId}`).then((res) => res.data),

  // the command comes from the db, so a run never races a stale editor value
  runStoredStep: (stepId) =>
    api
      .post(`${BASE}/steps/${stepId}/run`, { expected_step_fingerprint: null })
      .then((res) => res.data),

  // one phase checkbox flips dwell for the whole column
  updatePhaseTiming: (chainId, phase, enabled) =>
    api
      .patch(`${BASE}/chain/${chainId}/delay`, null, { params: { phase, enabled } })
      .then((res) => res.data),
}
