import { useEffect, useRef, useState } from 'react'
import { PlusIcon, XIcon } from '@phosphor-icons/react'

export const ChainPopup = ({ chains = [], onClose, onSelect, onCreate, onDelete }) => {
  const popupRef = useRef(null)
  const [newName, setNewName] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    const onClick = (e) => {
      // close when the click lands outside the popup
      if (popupRef.current && !popupRef.current.contains(e.target)) {
        onClose()
      }
    }

    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [onClose])

  // create chain by name, then select it to edit phases/steps
  const handleCreate = async (e) => {
    e.preventDefault()
    setError('')
    if (!newName.trim()) return

    setLoading(true)
    try {
      const created = await onCreate(newName.trim())
      setNewName('')
      onSelect(created.chain_id)
      onClose()
    } catch {
      setError('Chain name is taken or invalid')
    } finally {
      setLoading(false)
    }
  }

  const handleDelete = async (e, chainId, chainName) => {
    e.stopPropagation()
    if (!window.confirm(`Delete chain "${chainName}"?`)) return

    try {
      await onDelete(chainId)
    } catch {
      setError(`Could not delete ${chainName}`)
    }
  }

  const handleSelect = (chainId) => {
    onSelect(chainId)
    onClose()
  }

  return (
    <div
      ref={popupRef}
      className="absolute left-0 top-full z-[110] mt-1 w-[340px] rounded-lg border border-subtle bg-dropdown shadow-2xl shadow-black/40"
    >
      <div className="flex items-baseline justify-between border-b border-subtle px-3.5 py-2.5">
        <h3 className="text-[13px] font-medium text-primary">Chains</h3>
        <span className="font-mono text-[11px] text-muted">{chains.length}</span>
      </div>

      <div className="border-b border-subtle p-3">
        <form onSubmit={handleCreate} className="flex gap-2">
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="New chain name"
            className="h-8 min-w-0 flex-1 rounded-md border border-subtle bg-input px-2 text-xs text-primary outline-none transition-colors placeholder:text-muted focus:border-accent"
          />
          <button
            type="submit"
            disabled={loading || !newName.trim()}
            className="flex h-8 items-center gap-1.5 rounded-md bg-accent px-2.5 text-xs font-medium text-app transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:bg-default disabled:text-muted"
          >
            <PlusIcon size={14} />
            Create
          </button>
        </form>
      </div>

      {error && (
        <div className="border-b border-subtle px-3.5 py-2 text-xs text-danger">{error}</div>
      )}

      <div className="max-h-64 overflow-y-auto">
        {chains.length === 0 ? (
          <div className="px-3.5 py-3 text-xs text-muted">No chains yet</div>
        ) : (
          chains.map((c) => (
            <div
              key={c.chain_id}
              className="group flex items-center border-b border-subtle/60 last:border-b-0"
            >
              <button
                type="button"
                onClick={() => handleSelect(c.chain_id)}
                className="flex min-w-0 flex-1 items-baseline gap-2 px-3.5 py-2 text-left transition-colors hover:bg-card-hover"
              >
                <span className="truncate text-xs text-primary">{c.chain_name}</span>
                <span className="ml-auto shrink-0 text-[11px] text-muted">
                  {c.current_phase_name}
                </span>
              </button>
              <button
                type="button"
                onClick={(e) => handleDelete(e, c.chain_id, c.chain_name)}
                title={`Delete ${c.chain_name}`}
                className="mr-2 flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted opacity-0 transition-colors hover:bg-card-hover hover:text-danger group-hover:opacity-100 focus:opacity-100"
              >
                <XIcon size={14} />
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
