import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@/styles/lonetrail.css'
import '@/styles/decorators.css'
import '@/styles/motion.css'
import '@/styles/console.css'
import App from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
