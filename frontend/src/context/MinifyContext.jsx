import { useState, useCallback, useMemo } from 'react'
import { MinifyContext } from './minify-context.js'

export const MinifyProvider = ({ children }) => {
  const [minified, setMinified] = useState(false)
  const toggle = useCallback(() => setMinified((v) => !v), [])
  const value = useMemo(() => ({ minified, toggle }), [minified, toggle])
  return <MinifyContext.Provider value={value}>{children}</MinifyContext.Provider>
}
