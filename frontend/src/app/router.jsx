import { createBrowserRouter } from 'react-router-dom'
import { Login } from '@/pages/Login/Login'
import { ChainManagement } from '../pages/kill-chains/ChainManagement'

export const router = createBrowserRouter([
  {
    path: '/login',
    element: <Login />,
  },
  {
    path: '/',
    element: <ChainManagement />, // Unified Kill Chains list and editor
  },
])
