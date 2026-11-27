import type { LogEntry } from '../types/api'

type LogIdentity = Pick<LogEntry, 'id' | 'boot_id' | 'ts'>

function entryBootId(entry: LogIdentity | undefined) {
  return String(entry?.boot_id || '')
}

function latestBootId(list: readonly LogIdentity[]) {
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const boot = entryBootId(list[i])
    if (boot) return boot
  }
  return ''
}

function maxIdOf(list: readonly LogIdentity[]) {
  let maxId = 0
  for (const item of list) {
    const n = Number(item?.id || 0)
    if (n > maxId) maxId = n
  }
  return maxId
}

function maxTsOf(list: readonly LogIdentity[]) {
  let maxTs = 0
  for (const item of list) {
    const t = Date.parse(item?.ts || '') || 0
    if (t > maxTs) maxTs = t
  }
  return maxTs
}

function isNewBackendBoot(existing: readonly LogIdentity[], incoming: readonly LogIdentity[]) {
  if (!existing.length || !incoming.length) return false
  const inBoot = latestBootId(incoming)
  const exBoot = latestBootId(existing)
  if (inBoot && inBoot !== exBoot) return true
  if (!inBoot && !exBoot) {
    return maxIdOf(incoming) < maxIdOf(existing) && maxTsOf(incoming) > maxTsOf(existing)
  }
  return false
}

export function mergeLogsSnapshot<T extends LogIdentity>(existing: T[], incoming: T[], maxLogs = 500): T[] {
  const prev = Array.isArray(existing) ? existing : []
  if (!Array.isArray(incoming)) return prev
  if (incoming.length === 0) return []

  const replaceAll = isNewBackendBoot(prev, incoming)
  if (
    !replaceAll &&
    prev.length === incoming.length &&
    prev[0]?.id === incoming[0]?.id &&
    entryBootId(prev[0]) === entryBootId(incoming[0]) &&
    prev[prev.length - 1]?.id === incoming[incoming.length - 1]?.id &&
    entryBootId(prev[prev.length - 1]) === entryBootId(incoming[incoming.length - 1])
  ) {
    return prev
  }

  const map = new Map<number, T>()
  for (const item of incoming) {
    if (item && item.id != null) map.set(item.id, item)
  }

  if (!replaceAll) {
    const inBoot = latestBootId(incoming)
    const maxIncoming = maxIdOf(incoming)
    for (const cur of prev) {
      if (!cur || cur.id == null) continue
      const curBoot = entryBootId(cur)
      if (inBoot && curBoot && curBoot !== inBoot) continue
      if (Number(cur.id) > maxIncoming && !map.has(cur.id)) {
        map.set(cur.id, cur)
      }
    }
  }

  return Array.from(map.values())
    .sort((a, b) => Number(a.id) - Number(b.id))
    .slice(-maxLogs)
}

export function appendLiveLog<T extends LogIdentity>(existing: T[], entry: T, maxLogs = 500): T[] {
  const prev = Array.isArray(existing) ? existing : []
  if (!entry) return prev

  const inBoot = entryBootId(entry)
  const exBoot = latestBootId(prev)
  if (inBoot && prev.length > 0 && inBoot !== exBoot) {
    return [entry]
  }
  if (!inBoot && !exBoot && prev.length > 0 && entry.id != null) {
    const last = prev[prev.length - 1]
    if (Number(entry.id) < Number(last?.id || 0) && (Date.parse(entry.ts || '') || 0) > (Date.parse(last?.ts || '') || 0)) {
      return [entry]
    }
  }

  if (entry.id != null && prev.some((x) => x.id === entry.id && entryBootId(x) === inBoot)) {
    return prev
  }
  const next = [...prev, entry]
  if (next.length > maxLogs) next.splice(0, next.length - maxLogs)
  return next
}
