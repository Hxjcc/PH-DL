import type { WsMessage } from '../types/api'

interface EventStreamOptions {
  onEvent?: (message: Exclude<WsMessage, { type: 'ping' }>) => void
  onStatusChange?: (connected: boolean) => void
  onUnauthorized?: () => void
}

export class EventStreamClient {
  declare ws: WebSocket | null
  private declare onEvent: EventStreamOptions['onEvent']
  private declare onStatusChange: EventStreamOptions['onStatusChange']
  private declare onUnauthorized: EventStreamOptions['onUnauthorized']
  private declare stopped: boolean
  private declare retryMs: number
  private declare timer: ReturnType<typeof setTimeout> | null

  constructor({ onEvent, onStatusChange, onUnauthorized }: EventStreamOptions) {
    this.onEvent = onEvent
    this.onStatusChange = onStatusChange
    this.onUnauthorized = onUnauthorized
    this.ws = null
    this.stopped = true
    this.retryMs = 1000
    this.timer = null
  }

  connect() {
    if (!this.stopped) return
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

  private _open() {
    if (this.stopped) return
    this.timer = null
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/api/ws`
    const ws = new WebSocket(url)
    this.ws = ws

    ws.onopen = () => {
      if (this.ws !== ws || this.stopped) return
      this.retryMs = 1000
      this.onStatusChange?.(true)
    }

    ws.onmessage = (evt: MessageEvent<string>) => {
      if (this.ws !== ws || this.stopped) return
      try {
        const msg = JSON.parse(evt.data) as WsMessage
        if (msg.type !== 'ping') {
          this.onEvent?.(msg)
        }
      } catch {}
    }

    ws.onclose = (evt) => {
      if (this.ws !== ws || this.stopped) return
      this.ws = null
      this.onStatusChange?.(false)
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
