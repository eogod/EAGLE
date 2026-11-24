import { GlobalRegistrator } from '@happy-dom/global-registrator'

GlobalRegistrator.register({ url: 'http://localhost/' })

// React only wraps updates in act() when the environment says so
globalThis.IS_REACT_ACT_ENVIRONMENT = true
