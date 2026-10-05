import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import ClickSpark from './components/ClickSpark.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ClickSpark sparkColor="#12d9e8" sparkSize={10} sparkRadius={18} sparkCount={8} duration={450}>
      <App />
    </ClickSpark>
  </StrictMode>,
)
