let unauthorizedHandler = null

export function setUnauthorizedHandler(fn) {
  unauthorizedHandler = fn
}

const GATEWAY_HINTS = {
  502: '网关错误（502）',
  503: '服务暂不可用（503）',
  504: '网关超时（504）',
}

function formatHttpError(status, text) {
  const body = String(text || '').trim()
  const looksLikeHtml = /^<!doctype html/i.test(body) || /^<html[\s>]/i.test(body) || /<center>/i.test(body)
  if (looksLikeHtml) {
    const hint = GATEWAY_HINTS[status]
    if (hint) {
      return `${hint}：反向代理等待后端响应超时。登录可能仍在后台继续，请稍后刷新状态；如频繁出现，请把反向代理的 proxy_read_timeout 调到 600s 以上。`
    }
    return `服务器返回了网页而不是接口数据（HTTP ${status}），请检查反向代理是否正确转发 /api 请求。`
  }
  return body || `请求失败 (${status})`
}

async function request(path, options = {}) {
  const opts = {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
    ...options,
  }
  if (opts.body && typeof opts.body !== 'string') {
    opts.body = JSON.stringify(opts.body)
  }

  const resp = await fetch(path, opts)
  let data = null
  const text = await resp.text()
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = { detail: text }
    }
  }

  if (resp.status === 401) {
    if (unauthorizedHandler && !path.startsWith('/api/auth/')) {
      unauthorizedHandler()
    }
    throw new Error(formatHttpError(resp.status, data?.detail || text) || '未登录或会话已过期')
  }

  if (!resp.ok) {
    throw new Error(formatHttpError(resp.status, data?.detail || data?.message || text))
  }
  return data
}

export const api = {
  // Auth
  me: () => request('/api/auth/me'),
  login: (password) => request('/api/auth/login', { method: 'POST', body: { password } }),
  logout: () => request('/api/auth/logout', { method: 'POST' }),

  // Overview & Subscriptions
  overview: () => request('/api/overview'),
  subscriptions: (username = '', site = 'pornhub') => {
    const sp = new URLSearchParams()
    if (username) sp.set('username', username)
    if (site && site !== 'pornhub') sp.set('site', site)
    const qs = sp.toString() ? `?${sp.toString()}` : ''
    return request(`/api/subscriptions${qs}`)
  },

  // Targets
  listTargets: () => request('/api/targets'),
  createTarget: (payload) => request('/api/targets', { method: 'POST', body: payload }),
  batchCreateTargets: (payload) => request('/api/targets/batch', { method: 'POST', body: payload }),
  updateTarget: (id, payload) => request(`/api/targets/${id}`, { method: 'PATCH', body: payload }),
  deleteTarget: (id) => request(`/api/targets/${id}`, { method: 'DELETE' }),
  syncTargetBaseline: (id, pages = '') => {
    const qs = pages ? `?pages=${encodeURIComponent(pages)}` : ''
    return request(`/api/targets/${id}/sync-baseline${qs}`, { method: 'POST' })
  },
  runUpdate: (payload) => request('/api/targets/run-update', { method: 'POST', body: payload }),

  // Jobs
  listJobs: (limit = 30) => request(`/api/jobs?limit=${limit}`),
  createJob: (payload) => request('/api/jobs', { method: 'POST', body: payload }),
  getJob: (id) => request(`/api/jobs/${id}`),
  cancelJob: (id) => request(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  retryFailedJob: (id) => request(`/api/jobs/${id}/retry-failed`, { method: 'POST' }),
  deleteJob: (id) => request(`/api/jobs/${id}`, { method: 'DELETE' }),
  batchDeleteJobs: (jobIds) =>
    request('/api/jobs/batch-delete', { method: 'POST', body: { job_ids: jobIds } }),
  listTempFiles: () => request('/api/jobs/temp-files'),
  resumeTempFiles: () => request('/api/jobs/temp-files/resume', { method: 'POST' }),
  clearTempFiles: () => request('/api/jobs/temp-files', { method: 'DELETE' }),

  // Videos History
  listVideos: (params = {}, options = {}) => {
    const sp = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v !== '' && v !== null && v !== undefined) sp.set(k, String(v))
    }
    const reqOpts = options?.signal ? { signal: options.signal } : {}
    return request(`/api/videos?${sp.toString()}`, reqOpts)
  },
  videoPlayback: (viewkey, signal) =>
    request(`/api/videos/${encodeURIComponent(viewkey)}/playback`, { signal, cache: 'no-store' }),
  deleteVideo: (viewkey, deleteLocalFile = false) =>
    request(
      `/api/videos/${encodeURIComponent(viewkey)}${deleteLocalFile ? '?delete_local_file=true' : ''}`,
      { method: 'DELETE' }
    ),
  batchDeleteVideos: (viewkeys, deleteLocalFiles = false) =>
    request('/api/videos/batch-delete', {
      method: 'POST',
      body: { viewkeys, delete_local_files: Boolean(deleteLocalFiles) },
    }),
  redownloadVideo: (viewkey) =>
    request(`/api/videos/${encodeURIComponent(viewkey)}/redownload`, { method: 'POST' }),
  enrichBaselineMetadata: () => request('/api/videos/metadata/enrich', { method: 'POST' }),

  // Settings & Tools
  getSettings: () => request('/api/settings'),
  saveSettings: (patch) => request('/api/settings', { method: 'PUT', body: patch }),
  uploadAccountAvatar: (dataUrl) =>
    request('/api/settings/avatar', { method: 'POST', body: { data_url: dataUrl } }),
  deleteAccountAvatar: () => request('/api/settings/avatar', { method: 'DELETE' }),
  testCookie: () => request('/api/settings/cookie/test', { method: 'POST' }),
  importCookie: (cookie) => request('/api/settings/cookie/import', { method: 'POST', body: { cookie } }),
  refreshCookie: () => request('/api/settings/cookie/refresh', { method: 'POST' }),
  cookieRefreshStatus: () => request('/api/settings/cookie/refresh/status'),
  testXhCookie: () => request('/api/settings/xh-cookie/test', { method: 'POST' }),
  importXhCookie: (cookie) => request('/api/settings/xh-cookie/import', { method: 'POST', body: { cookie } }),
  refreshXhCookie: () => request('/api/settings/xh-cookie/refresh', { method: 'POST' }),
  xhCookieRefreshStatus: () => request('/api/settings/xh-cookie/refresh/status'),
  testYescaptcha: () => request('/api/settings/yescaptcha/test', { method: 'POST' }),
  listRcloneRemotes: () => request('/api/settings/rclone/remotes'),
  testRclone: () => request('/api/settings/rclone/test', { method: 'POST' }),
  testNotify: () => request('/api/settings/notify/test', { method: 'POST' }),
  getLogs: () => request('/api/settings/logs'),
  clearLogs: () => request('/api/settings/logs', { method: 'DELETE' }),
}
