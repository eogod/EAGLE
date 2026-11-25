export const proposalPart = (proposal) => ({
  type: 'edit-proposal',
  proposalId: proposal.public_id,
  kind: proposal.kind,
  status: proposal.status,
  proposal,
})

export function appendText(parts, text) {
  if (!text) return parts
  if (parts.length && parts[parts.length - 1].type === 'text') {
    const next = [...parts]
    next[next.length - 1] = {
      ...next[next.length - 1],
      text: `${next[next.length - 1].text}${text}`,
    }
    return next
  }
  return [...parts, { type: 'text', text }]
}

/** reduce one SSE event into assistant-ui content parts (mirrors the backend reducer) */
export function reduceParts(parts, event) {
  const payload = event.data || {}

  if (event.event === 'assistant.delta') {
    return appendText(parts, payload.text || '')
  }
  if (event.event === 'tool.started') {
    const args = payload.args || {}
    return [
      ...parts,
      {
        type: 'tool-call',
        toolCallId: String(payload.toolCallId || ''),
        toolName: String(payload.toolName || ''),
        args,
        argsText: JSON.stringify(args),
        isPreliminary: true,
      },
    ]
  }
  if (event.event === 'tool.completed') {
    return parts.map((part) => {
      if (part.type !== 'tool-call' || part.toolCallId !== String(payload.toolCallId)) {
        return part
      }
      const { isPreliminary: _preliminary, ...rest } = part
      return { ...rest, result: payload.result, isError: payload.status === 'error' }
    })
  }
  if (event.event === 'run.error') {
    return appendText(parts, `\n\n${payload.message || 'analysis failed'}`)
  }
  if (event.event === 'run.cancelled') {
    return appendText(parts, '\n\nRun cancelled.')
  }
  if (event.event === 'edit.proposed') {
    // the SSE payload and the stored part name the id differently
    const proposalId = String(payload.proposal_id || payload.public_id || '')
    if (!proposalId) return parts

    const part = {
      type: 'edit-proposal',
      proposalId,
      kind: payload.kind,
      status: payload.status,
      proposal: payload,
    }
    const existing = parts.findIndex(
      (item) => item.type === 'edit-proposal' && item.proposalId === proposalId,
    )
    if (existing === -1) return [...parts, part]

    const next = [...parts]
    next[existing] = part
    return next
  }
  return parts
}

/** a dropped stream must not hide an open review, so fold the API list back in */
export function mergeProposals(messages, proposals) {
  if (!proposals.length) return messages

  const seen = new Set()
  const merged = messages.map((message) => ({
    ...message,
    content: (message.content || []).map((part) => {
      if (part.type !== 'edit-proposal') return part
      const fresh = proposals.find((item) => item.public_id === part.proposalId)
      if (!fresh) return part
      seen.add(fresh.public_id)
      return proposalPart(fresh)
    }),
  }))

  const missing = proposals.filter((item) => !seen.has(item.public_id))
  if (!missing.length) return merged

  const cards = missing.map(proposalPart)
  const last = merged.at(-1)
  if (last?.role === 'assistant') {
    return [...merged.slice(0, -1), { ...last, content: [...last.content, ...cards] }]
  }
  return [...merged, { id: 'pending-proposals', role: 'assistant', content: cards }]
}
