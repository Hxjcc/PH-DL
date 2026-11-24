export class EventStreamClient {
  constructor({ onEvent, onStatusChange, onUnauthorized }) {
    this.onEvent = onEvent
    this.onStatusChange = onStatusChange
    this.onUnauthorized = onUnauthorized
    this.ws = null
    this.stopped = true
    this.retryMs = 1000
    this.timer = null
  }

  connect() {
    this.stopped = false
    this._open()
  }

  disconnect() {
    this.stopped = true
    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = null
    }
    if (this.ws) {
      try {
        this.ws.close()
      } catch {}
      this.ws = null
    }
    this.onStatusChange?.(false)
  }

  _open() {
    if (this.stopped) return
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/api/ws`
    const ws = new WebSocket(url)
    this.ws = ws

    ws.onopen = () => {
      this.retryMs = 1000
      this.onStatusChange?.(true)
    }

    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data)
        if (msg.type !== 'ping') {
          this.onEvent?.(msg)
        }
      } catch {}
    }

    ws.onclose = (evt) => {
      this.onStatusChange?.(false)
      if (this.stopped) return
      if (evt.code === 4401) {
        this.stopped = true
        this.onUnauthorized?.()
        return
      }
      const delay = this.retryMs
      this.retryMs = Math.min(this.retryMs * 1.8, 15000)
      this.timer = setTimeout(() => this._open(), delay)
    }

    ws.onerror = () => {
      try {
        ws.close()
      } catch {}
    }
  }
}
