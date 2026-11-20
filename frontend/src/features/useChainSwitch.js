import { useState, useCallback, useEffect } from 'react'
import { chainApi } from '../entities/chainApi'

// to cache chain choice
const LAST_CHAIN_KEY = 'eagle_last_chain_id'

export const useChainSwitch = () => {
  const [chains, setChains] = useState([])
  const [currentChain, setCurrentChain] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [initialized, setInitialized] = useState(false)

  const loadChains = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await chainApi.list()
      setChains(data)

      const lastId = localStorage.getItem(LAST_CHAIN_KEY)
      if (lastId) {
        const match = data.find((c) => c.chain_id === Number(lastId))
        if (match) {
          try {
            const full = await chainApi.getChainInfo(match.chain_id)
            setCurrentChain((prev) => prev ?? full)
          } catch {
            // the cached chain is gone; fall back to the picker silently
            localStorage.removeItem(LAST_CHAIN_KEY)
          }
        } else {
          localStorage.removeItem(LAST_CHAIN_KEY)
        }
      }
    } catch (err) {
      setError(err?.response?.data?.detail ?? 'Failed to load chains')
    } finally {
      setLoading(false)
      setInitialized(true)
    }
  }, [])

  // run loadChains after last sync task as Microtask, just on first render
  useEffect(() => {
    queueMicrotask(loadChains)
  }, [loadChains])

  const switchChain = useCallback(async (chainId) => {
    setLoading(true)
    setError(null)
    try {
      const data = await chainApi.getChainInfo(chainId)
      setCurrentChain(data)
      // save last working chain
      localStorage.setItem(LAST_CHAIN_KEY, chainId)
      return data
    } catch (err) {
      setError(err?.response?.data?.detail ?? 'Failed to load chain')
      throw err
    } finally {
      setLoading(false)
    }
  }, [])

  const createChain = useCallback(async (chainName) => {
    setLoading(true)
    setError(null)
    try {
      const data = await chainApi.create(chainName)
      // add new chain in list
      setChains((prev) => [data, ...prev])
      return data
    } catch (err) {
      setError(err?.response?.data?.detail ?? 'Failed to create chain')
      throw err
    } finally {
      setLoading(false)
    }
  }, [])

  const deleteChain = useCallback(async (chainId) => {
    setLoading(true)
    setError(null)
    try {
      await chainApi.delete(chainId)
      // update chain list by filter chain on != deleted
      setChains((prev) => prev.filter((c) => c.chain_id !== chainId))
      // cuz closure of value may be stale so prev
      // reads the latest state value, clears it if the deleted chain was active
      setCurrentChain((prev) => (prev?.chain_id === chainId ? null : prev))
      localStorage.removeItem(LAST_CHAIN_KEY)
    } catch (err) {
      setError(err?.response?.data?.detail ?? 'Failed to delete chain')
      throw err
    } finally {
      setLoading(false)
    }
  }, [])

  return {
    chains,
    currentChain,
    loading,
    error,
    initialized,
    loadChains,
    switchChain,
    createChain,
    deleteChain,
  }
}
