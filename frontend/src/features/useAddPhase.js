import { useState, useCallback } from 'react'
import { chainApi } from '../entities/chainApi'

export const useAddPhase = (chainId) => {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [currentPhase, setCurrentPhase] = useState(null)

  // same function reference between renders
  const addPhase = useCallback(
    async (phaseName) => {
      if (!chainId) return
      setLoading(true)
      setError(null)
      try {
        const data = await chainApi.setPhase(chainId, phaseName)
        setCurrentPhase(data.current_phase_name)
        return data
      } catch (err) {
        // we are not sure what it even exists
        setError(err?.response?.data?.detail ?? err.message)
        throw err
      } finally {
        setLoading(false)
      }
    },
    [chainId],
  )

  return { addPhase, loading, error, currentPhase }
}
