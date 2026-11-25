import { useContext } from 'react'
import { MinifyContext } from './minify-context.js'

export const useMinify = () => {
  const ctx = useContext(MinifyContext)
  if (!ctx) throw new Error('useMinify must be used within MinifyProvider')
  return ctx
}
