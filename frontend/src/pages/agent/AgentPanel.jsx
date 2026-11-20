import { useCallback, useEffect, useRef, useState } from 'react'
import { AssistantRuntimeProvider, ComposerPrimitive, ThreadPrimitive } from '@assistant-ui/react'
import {
  PaperclipIcon,
  SidebarSimpleIcon,
  SpinnerGapIcon,
  WarningCircleIcon,
} from '@phosphor-icons/react'
import { toast } from 'react-toastify'
import { agentApi } from '../../entities/agentApi'
import { useAgentSession, useAgentThread } from './useAgentThread'

const TOOL_LABELS = {
  extract_document: 'Reading report',
  propose_chain_draft: 'Drafting attack chain',
  websearch: 'Searching the web',
  webfetch: 'Reading page',
  get_cve_info: 'Looking up CVE',
  search_cves_by_cpe: 'Searching CVEs by CPE',
}

const RESEARCH_TOOLS = ['websearch', 'webfetch', 'get_cve_info', 'search_cves_by_cpe']

// one muted line per research tool: the call's target plus what came back
const researchDetail = (part) => {
  const args = part.args || {}
  const target = args.query ?? args.url ?? args.cve_id ?? args.cpe ?? ''
  const summary = String(part.result?.summary || '')
  if (part.toolName === 'websearch') {
    const count = summary.match(/Found (\d+)/)
    return count ? `${target} · ${count[1]} result(s)` : target
  }
  if (part.toolName === 'webfetch') {
    const chars = summary.match(/Length: (\d+)/)
    return chars ? `${target} · ${chars[1]} chars` : target
  }
  if (part.toolName === 'search_cves_by_cpe') {
    const count = summary.match(/CVEs: (\d+)/)
    return count ? `${target} · ${count[1]} total` : target
  }
  return target
}

const MODES = ['ask', 'plan', 'edit']
const MODE_HINTS = {
  ask: 'Ask about the chain or report',
  plan: 'Describe the chain to plan',
  edit: 'Tell the agent what to change or run',
}

const PROVIDER_KEY = 'eagle_agent_provider'
const MODEL_KEY = 'eagle_agent_model'
const MODE_KEY = 'eagle_agent_mode'

export const AgentPanel = ({
  chainId,
  chainName,
  collapsed,
  onToggleCollapse,
  onImportDraft,
  onChainChanged,
  timingEnabled,
  onToggleTiming,
  plannedMinutes,
}) => {
  const { sessionId, error: sessionError } = useAgentSession(chainId, chainName)

  if (collapsed) {
    return (
      <aside className="flex h-full w-full flex-col items-center border-r border-subtle bg-nav py-2">
        <button
          type="button"
          onClick={onToggleCollapse}
          title="Open agent"
          className="rounded px-1.5 py-1 text-[11px] font-medium text-accent hover:bg-card-hover"
        >
          A
        </button>
      </aside>
    )
  }

  return (
    <aside className="flex h-full min-h-0 w-full flex-col bg-nav">
      <header className="flex items-center justify-between border-b border-subtle px-3 py-2.5">
        <span className="text-[12px] font-medium text-secondary">Agent</span>
        <button
          type="button"
          onClick={onToggleCollapse}
          title="Collapse panel"
          className="rounded p-1 text-muted hover:bg-card-hover hover:text-primary"
        >
          <SidebarSimpleIcon size={14} />
        </button>
      </header>

      <div className="flex items-center gap-2 border-b border-subtle px-3 py-2">
        <label
          className="flex items-center gap-1.5 text-[11px] text-muted"
          title="Wait the drafted dwell before every batch step (20-90 min per chain)"
        >
          <input
            type="checkbox"
            checked={timingEnabled}
            onChange={onToggleTiming}
            className="accent-current"
          />
          Realistic timing
        </label>
        <span
          className="ml-auto font-mono text-[11px] tabular-nums text-muted"
          title="Planned dwell across the enabled steps"
        >
          {plannedMinutes}m
        </span>
      </div>

      {sessionError ? (
        <div className="p-3 text-xs text-danger">{sessionError}</div>
      ) : sessionId ? (
        <AgentChat
          sessionId={sessionId}
          chainName={chainName}
          onImportDraft={onImportDraft}
          onChainChanged={onChainChanged}
        />
      ) : (
        <div className="flex flex-1 items-center justify-center text-xs text-muted">
          Starting session…
        </div>
      )}
    </aside>
  )
}

const AgentChat = ({ sessionId, chainName, onImportDraft, onChainChanged }) => {
  const [providers, setProviders] = useState([])
  const [provider, setProvider] = useState(() => localStorage.getItem(PROVIDER_KEY) || 'ollama')
  const [model, setModel] = useState(() => localStorage.getItem(MODEL_KEY) || '')
  const [mode, setMode] = useState(() => {
    const stored = localStorage.getItem(MODE_KEY)
    return MODES.includes(stored) ? stored : 'plan'
  })
  const [importing, setImporting] = useState(false)

  // a model from another provider is not offered in the current list
  const currentProvider = providers.find((item) => item.id === provider)
  const models = currentProvider?.models ?? []
  const effectiveModel = models.includes(model)
    ? model
    : currentProvider?.default || models[0] || model

  const {
    runtime,
    draft,
    draftSource,
    error,
    document,
    uploading,
    upload,
    detach,
    markDraftImported,
  } = useAgentThread({
    sessionId,
    provider,
    model: effectiveModel,
    mode,
    onChainChanged,
  })

  const loadProviders = useCallback(async () => {
    try {
      const body = await agentApi.getProviders()
      setProviders(body.providers)

      if (!localStorage.getItem(PROVIDER_KEY) && body.default?.provider) {
        setProvider(body.default.provider)
      }
      if (!localStorage.getItem(MODEL_KEY)) {
        const initial = body.providers.find((item) => item.id === body.default?.provider)
        setModel(initial?.default || body.default?.model || '')
      }
    } catch (err) {
      toast.error(err.message || 'Could not load models')
    }
  }, [])

  useEffect(() => {
    queueMicrotask(loadProviders)
  }, [loadProviders])

  const pickProvider = (value) => {
    const entry = providers.find((item) => item.id === value)
    const nextModel = entry?.models.includes(model)
      ? model
      : entry?.default || entry?.models[0] || ''
    setProvider(value)
    setModel(nextModel)
    localStorage.setItem(PROVIDER_KEY, value)
    localStorage.setItem(MODEL_KEY, nextModel)
  }
  const pickModel = (value) => {
    setModel(value)
    localStorage.setItem(MODEL_KEY, value)
  }
  const pickMode = (value) => {
    setMode(value)
    localStorage.setItem(MODE_KEY, value)
  }

  const isImported = draft?.status === 'imported'
  const isPlan = draft?.status === 'plan'

  const importDraftById = useCallback(
    async (draftId) => {
      setImporting(true)

      try {
        const result = await agentApi.importDraft(draftId, chainName)
        markDraftImported()
        onImportDraft?.(result.steps, result.validation)
        toast.success(`Saved ${result.steps.length} draft step(s) to the chain`)
      } catch (err) {
        toast.error(err.response?.data?.detail?.message || err.message || 'Import failed')
      } finally {
        setImporting(false)
      }
    },
    [chainName, markDraftImported, onImportDraft],
  )

  const handleImport = () => {
    if (!draft || isImported) return

    importDraftById(draft.draft_id ?? draft.id)
  }

  // any proposal run saves itself, so the canvas always reflects one chain
  const autoImportedRef = useRef(null)
  useEffect(() => {
    if (mode === 'ask' || draftSource !== 'stream') return
    if (!draft?.draft_id || !['needs_review', 'plan'].includes(draft.status)) return
    if (!draft.steps?.length) return
    if (autoImportedRef.current === draft.draft_id) return

    autoImportedRef.current = draft.draft_id
    queueMicrotask(() => importDraftById(draft.draft_id))
  }, [draft, draftSource, mode, importDraftById])

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="flex min-h-0 flex-1 flex-col">
        <AgentControls
          providers={providers}
          provider={provider}
          model={effectiveModel}
          mode={mode}
          onProvider={pickProvider}
          onModel={pickModel}
          onMode={pickMode}
          onRefresh={loadProviders}
        />

        <ThreadPrimitive.Root className="flex min-h-0 flex-1 flex-col">
          <ThreadPrimitive.Viewport className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
            <ThreadPrimitive.Empty>
              <EmptyState />
            </ThreadPrimitive.Empty>
            <ThreadPrimitive.Messages>
              {({ message }) => <AgentMessage message={message} />}
            </ThreadPrimitive.Messages>
          </ThreadPrimitive.Viewport>

          <div className="border-t border-subtle p-2">
            {draft && (
              <div className="mb-2 rounded-lg border border-subtle bg-card p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-[12px] text-primary">
                      {isPlan ? 'Plan' : isImported ? 'Imported' : 'Draft'}
                      <span className="ml-2 font-mono text-[11px] text-muted">
                        {draft.steps?.length ?? 0} steps
                      </span>
                    </div>
                    <div className="mt-0.5 truncate text-[11px] text-muted">
                      {draft.status} · {draft.validation?.warnings?.length ?? 0} warnings
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={handleImport}
                    disabled={importing || isImported || !draft.steps?.length}
                    title={
                      isImported ? 'Already saved into this chain' : 'Save into the canvas now'
                    }
                    className="shrink-0 rounded-md border border-accent/40 px-2.5 py-1 text-[11px] text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:border-subtle disabled:text-muted"
                  >
                    {importing ? (
                      <SpinnerGapIcon size={14} className="animate-spin" />
                    ) : isImported ? (
                      'Imported'
                    ) : (
                      'Import'
                    )}
                  </button>
                </div>
                {draft.validation?.errors?.length > 0 && (
                  <div className="mt-2 text-[11px] text-danger">{draft.validation.errors[0]}</div>
                )}
              </div>
            )}

            {error && (
              <div className="mb-2 flex items-start gap-1.5 text-[11px] text-danger">
                <WarningCircleIcon size={14} className="mt-0.5 shrink-0" />
                <span className="min-w-0 break-words">{error}</span>
              </div>
            )}

            <Composer
              document={document}
              uploading={uploading}
              onUpload={upload}
              onDetach={detach}
              mode={mode}
            />
          </div>
        </ThreadPrimitive.Root>
      </div>
    </AssistantRuntimeProvider>
  )
}

const AgentControls = ({
  providers,
  provider,
  model,
  mode,
  onProvider,
  onModel,
  onMode,
  onRefresh,
}) => {
  const current = providers.find((item) => item.id === provider)
  const models = current?.models ?? []

  return (
    <div className="flex flex-col gap-2 border-b border-subtle px-3 py-2.5">
      <div className="flex gap-2">
        <select
          value={provider}
          onChange={(event) => onProvider(event.target.value)}
          aria-label="Provider"
          className="h-8 min-w-0 flex-1 rounded-md border border-subtle bg-input px-2 text-[11px] text-primary outline-none transition-colors focus:border-accent"
        >
          {providers.length === 0 && <option value={provider}>{provider}</option>}
          {providers.map((item) => (
            <option key={item.id} value={item.id} disabled={!item.available}>
              {item.label}
              {item.available ? '' : ' (offline)'}
            </option>
          ))}
        </select>
        <select
          value={model}
          onChange={(event) => onModel(event.target.value)}
          aria-label="Model"
          className="h-8 min-w-0 flex-[1.6] rounded-md border border-subtle bg-input px-2 font-mono text-[11px] text-primary outline-none transition-colors focus:border-accent"
        >
          {models.length === 0 && (
            <option value="">{current?.error ? 'unavailable' : 'no models'}</option>
          )}
          {models.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </div>

      <div className="flex items-center gap-2">
        <div className="flex rounded-md border border-subtle bg-card p-0.5">
          {MODES.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => onMode(item)}
              className={`rounded px-2 py-1 text-[11px] capitalize transition-colors ${
                mode === item ? 'bg-card-hover text-primary' : 'text-muted hover:text-secondary'
              }`}
            >
              {item}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={onRefresh}
          className="ml-auto rounded-md px-2 py-1 text-[11px] text-muted transition-colors hover:bg-card-hover hover:text-primary"
        >
          Refresh
        </button>
      </div>

      {current?.error && (
        <div className="truncate text-[11px] text-warning" title={current.error}>
          {current.label}: {current.error}
        </div>
      )}
    </div>
  )
}

const EmptyState = () => (
  <div className="px-0.5 py-6 text-[12px] leading-relaxed text-muted">
    <p className="text-secondary">Attach a DFIR report, or just describe the chain you want.</p>
    <p className="mt-2">
      Ask answers questions, Plan outlines stages and rationale, Build produces importable steps. A
      finding only enters a draft when its quote is verbatim in the report.
    </p>
  </div>
)

const AgentMessage = ({ message }) => {
  const isUser = message.role === 'user'
  return (
    <div
      className={`mb-3 text-[13px] leading-6 ${
        isUser ? 'rounded-md bg-card px-2.5 py-2 text-primary' : 'text-primary'
      }`}
    >
      {(message.content || []).map((part, index) => {
        if (part.type === 'text') {
          return (
            <p key={part.id ?? index} className="whitespace-pre-wrap break-words">
              {part.text}
            </p>
          )
        }
        if (part.type === 'tool-call') {
          return <ToolCard key={part.toolCallId ?? index} part={part} />
        }
        return null
      })}
    </div>
  )
}

const ToolCard = ({ part }) => {
  const running = Boolean(part.isPreliminary)
  const result = part.result || {}
  return (
    <div className="my-2 rounded-md border border-subtle bg-card px-2.5 py-2 text-[11px]">
      <div className="flex items-center gap-2">
        {running ? (
          <SpinnerGapIcon size={14} className="animate-spin text-muted" />
        ) : (
          <span className={part.isError ? 'text-danger' : 'text-success'}>●</span>
        )}
        <span className="text-secondary">
          {TOOL_LABELS[part.toolName] || part.toolName || 'step'}
        </span>
        {part.isError && <span className="text-danger">failed</span>}
      </div>
      {part.toolName === 'extract_document' && result.segments != null && (
        <div className="mt-1 text-muted">
          {result.pages ?? 0} page(s) · {result.characters ?? 0} chars ·{' '}
          {result.analyzed_segments ?? 0}/{result.segments} segments analyzed
        </div>
      )}
      {part.toolName === 'propose_chain_draft' && result.steps != null && (
        <div className="mt-1 text-muted">
          {result.steps} step(s) · {result.status}
        </div>
      )}
      {RESEARCH_TOOLS.includes(part.toolName) && (
        <div className="mt-1 truncate text-muted">{researchDetail(part)}</div>
      )}
    </div>
  )
}

const Composer = ({ document, uploading, onUpload, onDetach, mode }) => {
  const fileRef = useRef(null)

  return (
    <ComposerPrimitive.Root className="rounded-lg border border-subtle bg-input">
      <div className="flex items-center gap-2 px-2 pt-2 text-[11px]">
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="flex items-center gap-1.5 rounded-md px-1.5 py-0.5 text-[11px] text-muted transition-colors hover:bg-card-hover hover:text-primary"
        >
          <PaperclipIcon size={14} />
          Attach
        </button>
        <input
          ref={fileRef}
          type="file"
          hidden
          accept=".pdf,.txt,.md,.markdown,.log,.json,.csv"
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file) onUpload(file)
            event.target.value = ''
          }}
        />
        <span className="min-w-0 flex-1 truncate text-muted">
          {uploading
            ? 'uploading…'
            : document
              ? `${document.filename} · ${document.char_count ?? 0} chars`
              : 'No report attached'}
        </span>
        {document && (
          <button
            type="button"
            onClick={onDetach}
            title="Detach report and work from the brief only"
            className="rounded px-1 text-[11px] text-muted transition-colors hover:bg-card-hover hover:text-danger"
          >
            ✕
          </button>
        )}
      </div>

      <ComposerPrimitive.Input
        rows={2}
        placeholder={`${MODE_HINTS[mode]}…`}
        className="w-full resize-none bg-transparent px-2.5 py-2 text-[13px] text-primary outline-none placeholder:text-muted"
      />

      <div className="flex justify-end px-2 pb-2">
        <ComposerPrimitive.Send className="h-7 rounded-md bg-accent px-3 text-[11px] font-medium capitalize text-app transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:bg-default disabled:text-muted">
          {mode}
        </ComposerPrimitive.Send>
      </div>
    </ComposerPrimitive.Root>
  )
}
