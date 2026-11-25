import { useCallback, useEffect, useState } from 'react'
import { SpinnerGapIcon, WarningCircleIcon } from '@phosphor-icons/react'
import { agentApi } from '../../entities/agentApi'
import {
  KIND_LABELS,
  SETTLED,
  STATUS_STYLES,
  clock,
  describeOperation,
  errorText,
  millisLeft,
  proposalView,
} from './proposalView'

export const ProposalCard = ({ sessionId, part, onChanged }) => {
  const [fetched, setFetched] = useState(null)
  const [gone, setGone] = useState(null)
  const [loading, setLoading] = useState(!part.proposal)
  const [busy, setBusy] = useState('')
  const [failure, setFailure] = useState(null)
  const [acknowledged, setAcknowledged] = useState(false)
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  const [now, setNow] = useState(() => Date.now())

  // persisted parts carry ids only, so load the payload behind them
  useEffect(() => {
    if (part.proposal) return

    let alive = true
    const fetchProposal = async () => {
      try {
        const body = await agentApi.getProposal(sessionId, part.proposalId)
        if (alive) setFetched(body)
      } catch (err) {
        if (alive) setFailure(errorText(err))
      } finally {
        if (alive) setLoading(false)
      }
    }
    queueMicrotask(fetchProposal)
    return () => {
      alive = false
    }
  }, [sessionId, part.proposal, part.proposalId])

  // live snapshots win until this card settles on its own answer
  const proposal = SETTLED.includes(fetched?.status) ? fetched : (part.proposal ?? fetched)
  const status = gone ?? proposal?.status

  const expiresAt = proposal?.expires_at
  useEffect(() => {
    if (!expiresAt) return

    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [expiresAt])

  const left = millisLeft(expiresAt, now)

  const run = useCallback(
    async (name, call) => {
      setBusy(name)
      setFailure(null)
      try {
        const updated = await call()
        setFetched(
          updated?.public_id ? updated : await agentApi.getProposal(sessionId, part.proposalId),
        )
        if (name === 'reject') setRejecting(false)
        onChanged?.()
      } catch (err) {
        const code = err.response?.status
        setFailure(errorText(err))
        if (code === 409) {
          // the chain moved; show whatever the server now says about this proposal
          try {
            setFetched(await agentApi.getProposal(sessionId, part.proposalId))
          } catch {
            // keep the 409 message when the refresh fails too
          }
        } else if ([404, 410].includes(code)) {
          setGone(code === 404 ? 'missing' : 'expired')
        }
      } finally {
        setBusy('')
      }
    },
    [sessionId, part.proposalId, onChanged],
  )

  if (loading) {
    return (
      <div className="my-2 flex items-center gap-2 rounded-md border border-subtle bg-card px-2.5 py-2 text-[11px] text-muted">
        <SpinnerGapIcon size={14} className="animate-spin" />
        Loading proposal…
      </div>
    )
  }

  if (!proposal) {
    return (
      <div className="my-2 flex items-start gap-1.5 rounded-md border border-subtle bg-card px-2.5 py-2 text-[11px] text-danger">
        <WarningCircleIcon size={14} className="mt-0.5 shrink-0" />
        <span className="min-w-0 break-words">
          {failure || `Proposal ${part.proposalId} is unavailable.`}
        </span>
      </div>
    )
  }

  const operations = proposal.operations ?? []
  const errors = proposal.validation?.errors ?? []
  const warnings = proposal.validation?.warnings ?? []
  const dangerous = proposal.validation?.dangerous_steps ?? []
  const view = proposalView(proposal, { left, acknowledged, busy, status })

  return (
    <div className="my-2 rounded-md border border-subtle bg-card px-2.5 py-2 text-[11px]">
      <div className="flex items-center gap-2">
        <span className="text-secondary">{KIND_LABELS[proposal.kind] || 'Proposal'}</span>
        <span className={`font-mono ${STATUS_STYLES[status] || 'text-muted'}`}>{status}</span>
        {left != null && view.unsettled && (
          <span
            className={`ml-auto font-mono tabular-nums ${view.expired ? 'text-danger' : 'text-muted'}`}
          >
            {view.expired ? 'expired' : `expires in ${clock(left)}`}
          </span>
        )}
      </div>

      {proposal.summary && (
        <p className="mt-1 whitespace-pre-wrap break-words text-primary">{proposal.summary}</p>
      )}

      {operations.length > 0 && (
        <ul className="mt-1.5 space-y-1">
          {operations.map((operation, index) => {
            const described = describeOperation(operation)
            return (
              <li key={`${operation.op}-${index}`} className="min-w-0">
                <div className="text-secondary">{described.title}</div>
                {described.lines.map((line) => (
                  <div key={line} className="whitespace-pre-wrap break-words font-mono text-muted">
                    {line}
                  </div>
                ))}
              </li>
            )
          })}
        </ul>
      )}

      {errors.length > 0 && (
        <ul className="mt-1.5 space-y-0.5 text-danger">
          {errors.map((item) => (
            <li key={item} className="break-words">
              {item}
            </li>
          ))}
        </ul>
      )}
      {warnings.length > 0 && (
        <ul className="mt-1.5 space-y-0.5 text-warning">
          {warnings.map((item) => (
            <li key={item} className="break-words">
              {item}
            </li>
          ))}
        </ul>
      )}
      {dangerous.length > 0 && (
        <div className="mt-1.5 text-danger">
          Dangerous steps: {dangerous.join(', ')}
          <label className="mt-1 flex items-center gap-1.5 text-warning">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
              className="accent-current"
            />
            I reviewed the dangerous steps
          </label>
        </div>
      )}

      {proposal.error && <div className="mt-1.5 break-words text-danger">{proposal.error}</div>}
      {failure && <div className="mt-1.5 break-words text-danger">{failure}</div>}

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {view.canApprove && (
          <ActionButton
            label="Approve"
            busy={busy === 'approve'}
            disabled={view.blocked}
            onClick={() =>
              run('approve', () =>
                agentApi.approveProposal(sessionId, part.proposalId, acknowledged),
              )
            }
          />
        )}
        {view.canApply && (
          <ActionButton
            label="Apply"
            busy={busy === 'apply'}
            disabled={view.blocked}
            onClick={() =>
              run('apply', () => agentApi.applyProposal(sessionId, part.proposalId, acknowledged))
            }
          />
        )}
        {view.canExecute && (
          <ActionButton
            label="Execute"
            busy={busy === 'execute'}
            disabled={view.blocked}
            onClick={() =>
              run('execute', () =>
                agentApi.executeProposal(sessionId, part.proposalId, acknowledged),
              )
            }
          />
        )}
        {view.canReject && (
          <button
            type="button"
            onClick={() => setRejecting((value) => !value)}
            disabled={view.busy}
            className="rounded-md border border-subtle px-2 py-0.5 text-muted transition-colors hover:border-danger/40 hover:text-danger disabled:cursor-not-allowed disabled:text-muted"
          >
            Reject
          </button>
        )}
        {view.expired && status === 'proposed' && (
          <span className="text-danger">This proposal expired; ask the agent again.</span>
        )}
      </div>

      {rejecting && (
        <div className="mt-1.5 flex items-center gap-1.5">
          <input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Why not now?"
            maxLength={500}
            className="h-6 min-w-0 flex-1 rounded border border-subtle bg-input px-1.5 text-[11px] text-primary outline-none focus:border-accent"
          />
          <ActionButton
            label="Confirm"
            busy={busy === 'reject'}
            disabled={Boolean(busy)}
            onClick={() =>
              run('reject', () =>
                agentApi.rejectProposal(sessionId, part.proposalId, reason.trim()),
              )
            }
          />
        </div>
      )}
    </div>
  )
}

const ActionButton = ({ label, busy, disabled, onClick }) => (
  <button
    type="button"
    onClick={onClick}
    disabled={disabled}
    className="rounded-md border border-accent/40 px-2 py-0.5 text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:border-subtle disabled:text-muted"
  >
    {busy ? <SpinnerGapIcon size={12} className="animate-spin" /> : label}
  </button>
)
