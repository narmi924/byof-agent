import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { beforeEach, afterEach, vi } from 'vitest'

// jsdom has no layout observer; Radix scroll areas need the browser API shape.
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  sessionStorage.clear()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  // View state lives in the address bar and must be reset between tests, otherwise one test's view leaks into the next.
  window.history.replaceState(null, '', '/')
})
