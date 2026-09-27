import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { AssistantText } from './AssistantText'

it('renders cost tables and action lists in answers as semantic elements and never runs model HTML', () => {
  const { container } = render(<AssistantText value={'## Plan\n- **Resupply** 800 EA\n- Wait for the receipt\n\n| Cost | Benefit |\n| --- | --- |\n| 120 | 240 |\n<script>alert(1)</script>'} />)
  expect(screen.getByRole('heading', { name: 'Plan' })).toBeVisible()
  expect(screen.getAllByRole('listitem')).toHaveLength(2)
  expect(screen.getByRole('cell', { name: '240' })).toBeVisible()
  expect(container.querySelector('script')).toBeNull()
  expect(screen.getByText('<script>alert(1)</script>')).toBeVisible()
})
