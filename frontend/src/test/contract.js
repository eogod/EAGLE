import contract from '../../contracts/api-contract.json'

const escapeSegment = (segment) => segment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const pattern = (path) =>
  new RegExp(
    `^${path
      .split('/')
      .map((segment) => (/^\{.+\}$/.test(segment) ? '[^/]+' : escapeSegment(segment)))
      .join('/')}$`,
  )

/** the api instance strips /api from urls, so compare the tail only */
const normalize = (url) => url.split('?')[0].replace(/^\/api/, '')

export const findContractEndpoint = (method, url) =>
  contract.endpoints.find(
    (item) =>
      item.method === method.toLowerCase() && pattern(normalize(item.path)).test(normalize(url)),
  )

export const expectContractEndpoint = (method, url) => {
  const match = findContractEndpoint(method, url)
  if (!match) {
    const known = contract.endpoints.map((item) => `${item.method} ${item.path}`).join('\n')
    throw new Error(`${method} ${url} is not in the frontend contract:\n${known}`)
  }
  return match
}

export const requestFields = (schema) => contract.requests[schema]

export const responseFields = (schema) => contract.responses[schema]
