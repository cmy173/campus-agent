import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from 'react-router-dom'
import { AppProviders } from './app/providers'
import { router } from './app/router'
import './index.css'
import { apiRequest } from './lib/api/http'
import { configureUploadLimit } from './lib/upload'
void apiRequest<{ upload_mb: number }>('/preferences/file-limits')
  .then(value => configureUploadLimit(value.upload_mb)).catch(() => undefined)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
)
