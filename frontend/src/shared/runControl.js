// Stop has to reach the phase loop in the browser: the backend can kill the
// running process, but only the client knows it should not submit the next step.
let stopped = false

export const runControl = {
  reset() {
    stopped = false
  },
  stop() {
    stopped = true
  },
  get stopped() {
    return stopped
  },
}
