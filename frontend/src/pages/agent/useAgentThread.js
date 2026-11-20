import { useCallback, useEffect, useRef, useState } from 'react'
import { useExternalStoreRuntime } from '@assistant-ui/react'
import { agentApi } from '../../entities/agentApi'

// stored drafts keep the chain payload nested; SSE drafts are already flat
const normalizeDraft = (stored) =>
  stored?.payload
    ? {
        ...stored.payload,
        draft_id: stored.id,
        status: stored.status ?? stored.payload.status,
      }
    : stored

const toUiMessage = (message) => ({
  id: String(message.id),
  role: message.role,
  content: message.content,
})

function appendText(parts, text) {
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
  return parts
}

function applyEvent(messages, assistantId, event) {
  return messages.map((message) =>
    message.id === assistantId
      ? { ...message, content: reduceParts(message.content, event) }
      : message,
  )
}

/** one agent session per chain, created on first use */
export const useAgentSession = (chainId, chainName) => {
  // resolved.chainId lets us ignore a stale session without a synchronous reset
  const [resolved, setResolved] = useState({ chainId: null, id: null })
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!chainId) return

    let alive = true
    const resolve = async () => {
      try {
        const sessions = await agentApi.listSessions()
        let session = sessions.find((item) => item.chain_id === chainId)
        if (!session) {
          session = await agentApi.createSession({ chain_id: chainId, title: chainName })
        }
        if (!alive) return

        setError(null)
        setResolved({ chainId, id: session.id })
      } catch (err) {
        if (alive) setError(err.message)
      }
    }
    queueMicrotask(resolve)
    return () => {
      alive = false
    }
  }, [chainId, chainName])

  const sessionId = resolved.chainId === chainId ? resolved.id : null
  return { sessionId, error }
}

export const useAgentThread = ({
  sessionId,
  provider,
  model,
  mode,
  onDraftReady,
  onChainChanged,
}) => {
  const [messages, setMessages] = useState([])
  const [isRunning, setIsRunning] = useState(false)
  const [draft, setDraft] = useState(null)
  const [draftSource, setDraftSource] = useState('none')
  const [error, setError] = useState(null)
  const [document, setDocument] = useState(null)
  const [uploading, setUploading] = useState(false)

  const abortRef = useRef(null)
  const runIdRef = useRef(null)
  const documentRef = useRef(null)

  const load = useCallback(async () => {
    if (!sessionId) return

    try {
      const session = await agentApi.getSession(sessionId)
      setMessages((session.messages || []).map(toUiMessage))
      setDraft(normalizeDraft(session.draft))
      setDraftSource(session.draft ? 'stored' : 'none')

      const latest = (session.documents || []).at(-1)
      if (latest && !documentRef.current) {
        documentRef.current = latest
        setDocument(latest)
      }
    } catch (err) {
      setError(err.message)
    }
  }, [sessionId])

  useEffect(() => {
    queueMicrotask(load)
  }, [load])

  const upload = useCallback(
    async (file) => {
      if (!file || !sessionId) return

      setUploading(true)
      setError(null)
      try {
        const uploaded = await agentApi.uploadDocument(sessionId, file)
        documentRef.current = uploaded
        setDocument(uploaded)
      } catch (err) {
        setError(err.message)
      } finally {
        setUploading(false)
      }
    },
    [sessionId],
  )

  const onNew = useCallback(
    async (message) => {
      const text = (message.content || [])
        .filter((part) => part.type === 'text')
        .map((part) => part.text)
        .join('\n')
        .trim()
      if (!text) return
      const activeDocument = documentRef.current

      const stamp = Date.now()
      const assistantId = `assistant-${stamp}`

      setError(null)
      setMessages((prev) => [
        ...prev,
        { id: `user-${stamp}`, role: 'user', content: message.content },
        { id: assistantId, role: 'assistant', content: [] },
      ])

      const controller = new AbortController()
      abortRef.current = controller
      setIsRunning(true)

      try {
        for await (const event of agentApi.streamMessage(
          sessionId,
          {
            content: text,
            document_id: activeDocument?.id ?? null,
            provider: provider || null,
            model: model || null,
            mode,
          },
          controller.signal,
        )) {
          setMessages((prev) => applyEvent(prev, assistantId, event))

          if (event.event === 'run.started') runIdRef.current = event.data.run_id
          if (event.event === 'run.error') setError(event.data.message || 'analysis failed')
          if (event.event === 'edit.applied') onChainChanged?.()
          if (event.event === 'draft.ready') {
            setDraft(normalizeDraft(event.data.draft))
            setDraftSource('stream')
            onDraftReady?.(event.data.draft)
          }
          if (['run.completed', 'run.failed', 'run.cancelled'].includes(event.event)) break
        }
      } catch (err) {
        if (err.name !== 'AbortError') {
          setError(err.message)
          setMessages((prev) =>
            applyEvent(prev, assistantId, {
              event: 'run.error',
              data: { message: err.message },
            }),
          )
        }
      } finally {
        setIsRunning(false)
        abortRef.current = null
        runIdRef.current = null

        await load()
      }
    },
    [sessionId, load, onDraftReady, onChainChanged, provider, model, mode],
  )

  const detach = useCallback(() => {
    documentRef.current = null
    setDocument(null)
  }, [])

  const markDraftImported = useCallback(() => {
    setDraft((current) => (current ? { ...current, status: 'imported' } : current))
  }, [])

  const onCancel = useCallback(async () => {
    abortRef.current?.abort()
    const runId = runIdRef.current
    if (!runId) return

    try {
      await agentApi.cancelRun(runId)
    } catch {
      // the stream may already be closed; nothing else to do
    }
  }, [])

  const runtime = useExternalStoreRuntime({
    messages,
    isRunning,
    convertMessage: (message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
    }),
    onNew,
    onCancel,
  })

  return {
    runtime,
    draft,
    draftSource,
    error,
    document,
    uploading,
    upload,
    detach,
    markDraftImported,
    isRunning,
    clearError: () => setError(null),
  }
}
