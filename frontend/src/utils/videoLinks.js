const videoKeyPattern = /(?:viewkey=|\/embed\/|\/shorties\/)([a-zA-Z0-9]+)/

export function parseVideoLinks(text) {
  const videos = new Map()
  let invalid = 0
  let duplicates = 0
  for (const row of text.split(/\r?\n/)) {
    const line = row.trim()
    if (!line || line.startsWith('#')) continue

    const key = line.match(videoKeyPattern)?.[1]
    let valid = false
    if (key && /^https?:\/\//i.test(line) && !/[\s\\]/.test(line)) {
      try {
        const host = new URL(line).hostname.replace(/\.+$/, '')
        valid = host === 'pornhub.com' || host.endsWith('.pornhub.com')
      } catch {}
    }
    if (!valid) {
      invalid += 1
    } else if (videos.has(key)) {
      duplicates += 1
    } else {
      videos.set(key, line)
    }
  }
  return { urls: [...videos.values()], invalid, duplicates }
}
