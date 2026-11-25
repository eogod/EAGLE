import { useCallback, useEffect, useRef, useState } from 'react'
import { useExternalStoreRuntime } from '@assistant-ui/react'
import { agentApi } from '../../entities/agentApi'
import { mergeProposals, reduceParts } from './agentParts'

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

const fetchPendingProposals = async (sessionId) => {
  try {
    const body = await agentApi.listProposals(sessionId)
    // the list endpoint answers with a bare array or a named collection
    return Array.isArray(body) ? body : (body?.proposals ?? [])
  } catch {
    // a missing endpoint only costs the fallback cards, not the conversation
    return []
  }
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
  const [pendingProposals, setPendingProposals] = useState([])
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
      const pending = await fetchPendingProposals(sessionId)
      setMessages(mergeProposals((session.messages || []).map(toUiMessage), pending))
      setPendingProposals(pending)
      setDraft(normalizeDraft(session.draft))

      const latest = (session.documents || []).at(-1)
      if (latest && !documentRef.current) {
        documentRef.current = latest
        setDocument(latest)
      }
    } catch (err) {
      setError(err.message)
    }
  }, [sessionId])

  const refreshProposals = useCallback(async () => {
    if (!sessionId) return

    const pending = await fetchPendingProposals(sessionId)
    setPendingProposals(pending)
    setMessages((prev) => mergeProposals(prev, pending))
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
          // edit.applied no longer streams; kept so an older backend still reloads
          if (event.event === 'edit.applied') onChainChanged?.()
          if (event.event === 'draft.ready') {
            setDraft(normalizeDraft(event.data.draft))
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
    // the runtime only accepts known part types, so proposals travel as data parts
    convertMessage: (message) => ({
      id: message.id,
      role: message.role,
      content: (message.content || []).map((part) =>
        part.type === 'edit-proposal' ? { type: 'data', name: 'edit-proposal', data: part } : part,
      ),
    }),
    onNew,
    onCancel,
  })

  return {
    runtime,
    draft,
    error,
    document,
    uploading,
    upload,
    detach,
    markDraftImported,
    pendingProposals,
    refreshProposals,
    isRunning,
    clearError: () => setError(null),
  }
}
