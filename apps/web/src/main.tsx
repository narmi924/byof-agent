import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import './styles.css'
import './board.css'
import './assistant.css'
import './factoryOverview.css'
import './factoryEditor.css'
import './globals.css'

const root = document.getElementById('root')
if (!root) throw new Error('The workbench mount node is missing')

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
