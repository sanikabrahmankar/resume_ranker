import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'

// Import Bootstrap CSS globally
import 'bootstrap/dist/css/bootstrap.min.css'

// This is the critical command that actually draws the app on the screen
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)