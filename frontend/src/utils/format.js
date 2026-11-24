export function formatBytes(bytes) {
  const n = Number(bytes || 0)
  if (n <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let idx = 0
  let val = n
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024
    idx += 1
  }
  return `${val >= 100 ? val.toFixed(0) : val.toFixed(1)} ${units[idx]}`
}

export function formatSpeed(bps) {
  const n = Number(bps || 0)
  if (n <= 0) return ''
  return `${formatBytes(n)}/s`
}

export function formatLogTime(iso) {
  if (!iso) return '--:--:--'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso).slice(11, 19)
  const pad = (v) => String(v).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export function formatLogTimestamp(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso).replace('T', ' ').slice(0, 19)
  const pad = (v) => String(v).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${formatLogTime(iso)}`
}

export function formatDateTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso).replace('T', ' ').slice(0, 19)
  const pad = (v) => String(v).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function jobStatusMeta(status) {
  const map = {
    queued: { label: '排队中', dot: 'bg-amber-400', variant: 'outline' },
    scanning: { label: '扫描解析中', dot: 'bg-brand status-dot-glow-brand animate-pulse', variant: 'secondary' },
    running: { label: '执行中', dot: 'bg-brand status-dot-glow-brand animate-pulse', variant: 'secondary' },
    completed: { label: '已完成', dot: 'bg-emerald-500', variant: 'outline' },
    partial_failed: { label: '部分失败', dot: 'bg-amber-500', variant: 'outline' },
    cancelled: { label: '已取消', dot: 'bg-zinc-400', variant: 'outline' },
    failed: { label: '执行失败', dot: 'bg-red-500', variant: 'destructive' },
  }
  return map[status] || { label: status || '未知', dot: 'bg-zinc-400', variant: 'outline' }
}

export function itemStatusMeta(status) {
  const map = {
    pending: { label: '等待', dot: 'bg-zinc-400' },
    downloading: { label: '下载中', dot: 'bg-brand status-dot-glow-brand animate-pulse' },
    downloaded: { label: '已落盘', dot: 'bg-sky-500' },
    uploading: { label: '上传中', dot: 'bg-amber-500 status-dot-glow-amber animate-pulse' },
    done: { label: '已上传', dot: 'bg-emerald-500' },
    skipped: { label: '跳过', dot: 'bg-zinc-400' },
    failed: { label: '失败', dot: 'bg-red-500' },
    cancelled: { label: '取消', dot: 'bg-zinc-400' },
  }
  return map[status] || { label: status || '未知', dot: 'bg-zinc-400' }
}

export function videoStatusMeta(status) {
  const map = {
    uploaded: { label: '已上传云端', dot: 'bg-emerald-500' },
    downloaded: { label: '本地已下载', dot: 'bg-sky-500' },
    baseline: { label: '仅基线去重', dot: 'bg-zinc-400' },
  }
  return map[status] || { label: status || '未知', dot: 'bg-zinc-400' }
}

export function targetTypeMeta(targetType) {
  const map = {
    model: 'Model',
    pornstar: 'Pornstar',
    users: 'User',
    favorites: '收藏夹',
    liked: '我的点赞',
    auto: '自动',
  }
  return map[targetType] || targetType || '未知'
}

const AVATAR_PALETTES = [
  'from-zinc-700 to-zinc-900 dark:from-zinc-700 dark:to-zinc-800',
  'from-neutral-700 to-zinc-900 dark:from-zinc-700 dark:to-neutral-800',
  'from-stone-700 to-zinc-900 dark:from-stone-700 dark:to-zinc-800',
  'from-slate-700 to-zinc-900 dark:from-slate-700 dark:to-zinc-800',
]

export function avatarGradient(seed) {
  const s = String(seed || 'phdl')
  let hash = 0
  for (let i = 0; i < s.length; i += 1) {
    hash = (hash * 31 + s.charCodeAt(i)) >>> 0
  }
  return AVATAR_PALETTES[hash % AVATAR_PALETTES.length]
}
