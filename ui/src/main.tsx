import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { InboxRoute } from './routes/InboxRoute'
import { SkillsIndex } from './routes/SkillsIndex'
import { SkillDetail } from './routes/SkillDetail'
import { CaseDetail } from './routes/CaseDetail'
import { RunsIndex } from './routes/RunsIndex'
import { RunDetail } from './routes/RunDetail'
import { Triage } from './routes/Triage'
import { ReviewsIndex } from './routes/ReviewsIndex'
import { ReviewDetail } from './routes/ReviewDetail'
import './index.css'

const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      // The inbox is home: the console opens on what needs doing, not on a list of what exists.
      { index: true, element: <InboxRoute /> },
      { path: 'skills', element: <SkillsIndex /> },
      { path: 'skills/:skillId', element: <SkillDetail /> },
      { path: 'skills/:skillId/cases/:caseId', element: <CaseDetail /> },
      { path: 'reviews', element: <ReviewsIndex /> },
      { path: 'reviews/:reviewId', element: <ReviewDetail /> },
      { path: 'triage', element: <Triage /> },
      { path: 'runs', element: <RunsIndex /> },
      { path: 'runs/:runId', element: <RunDetail /> },
    ],
  },
])

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Skills are read from disk per request, so refetching on focus picks up edits made in an
      // editor without a manual reload.
      refetchOnWindowFocus: true,
      retry: 1,
      staleTime: 5_000,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
