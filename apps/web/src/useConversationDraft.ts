import { useCallback, useEffect, useState } from 'react'
import type { SetStateAction } from 'react'

function read(key: string): string {
  try { return sessionStorage.getItem(key)?.slice(0, 8000) ?? '' } catch { return '' }
}

export function useConversationDraft(scope: string) {
  const key = `byof.draft.v1:${scope}`
  const [edits, setEdits] = useState<Record<string, string>>({})
  const value = edits[key] ?? read(key)
  const setForScope = useCallback((targetScope: string, update: SetStateAction<string>) => {
    const target = `byof.draft.v1:${targetScope}`
    setEdits(current => {
      const next = (typeof update === 'function' ? update(current[target] ?? read(target)) : update).slice(0, 8000)
      return { ...current, [target]: next }
    })
  }, [])
  const setValue = useCallback((update: SetStateAction<string>) => setForScope(scope, update), [scope, setForScope])
  // Session-only: no cross-user draft sharing or indefinite local retention.
  // Persist outside the state updater, which React can invoke more than once.
  useEffect(() => {
    try { if (value) sessionStorage.setItem(key, value); else sessionStorage.removeItem(key) } catch { /* In-memory editing remains available. */ }
  }, [key, value])
  return [value, setValue, setForScope] as const
}
