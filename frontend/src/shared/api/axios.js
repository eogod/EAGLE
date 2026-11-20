import axios from 'axios'

// session lives in HttpOnly cookies set by the backend
export const api = axios.create({
  baseURL: '/api',
  withCredentials: true,
})

// one shared refresh, so parallel 401s do not race two rotations
// (a lost race would trip the reused-token family wipe on the server)
let refreshInFlight = null

const runSilentRefresh = () => {
  if (refreshInFlight) return refreshInFlight
  // no body: the backend takes the refresh token from the HttpOnly cookie;
  // a body must be a full RefreshTokenRequest, so `{_retried: true}` is a 422
  refreshInFlight = api
    .post('/auth/refresh-token')
    .then(() => true)
    .catch(() => false)
    .finally(() => {
      refreshInFlight = null
    })
  return refreshInFlight
}

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const config = error.config
    const status = error.response?.status
    // auth calls fail for real (bad password / replayed refresh), straight to login
    if (
      status === 401 &&
      config &&
      !config._retried &&
      !['/auth/access-token', '/auth/refresh-token', '/auth/logout'].includes(config.url)
    ) {
      config._retried = true
      if (await runSilentRefresh()) {
        return api.request(config)
      }
    }
    if (status === 401) {
      window.location.href = '/login'
    }
    return Promise.reject(error)
  },
)
