const phVideoKeyPattern = /(?:viewkey=|\/embed\/|\/shorties\/)([a-zA-Z0-9]+)/
const xhHostPattern = /^(?:[a-z0-9-]+\.)*xhamster\d*\.(?:com|desi|one|tv|xxx|live)$/i

function extractXhamsterVideoKey(pathname) {
  const parts = String(pathname || '')
    .split('/')
    .filter(Boolean)
  if (parts.length < 2) return ''
  const head = parts[0].toLowerCase()
  const slug = parts[1].trim()
  if (head === 'videos' || head === 'movies') {
    const hashMatch = slug.match(/(?:^|-)(xh[a-zA-Z0-9]{4,})$/)
    if (hashMatch?.[1]) return hashMatch[1]
    const numMatch = slug.match(/(?:^|-)(\d{4,})$/)
    if (numMatch?.[1]) return `xh_${numMatch[1]}`
    return ''
  }
  if (head === 'embed') {
    if (/^xh[a-zA-Z0-9]{4,}$/.test(slug)) return slug
    if (/^\d{4,}$/.test(slug)) return `xh_${slug}`
  }
  return ''
}

export function parseVideoLinks(text) {
  const videos = new Map()
  let invalid = 0
  let duplicates = 0
  for (const row of String(text || '').split(/\r?\n/)) {
    const line = row.trim()
    if (!line || line.startsWith('#')) continue

    let key = ''
    let valid = false
    if (/^https?:\/\//i.test(line) && !/[\s\\]/.test(line)) {
      try {
        const parsed = new URL(line)
        const host = parsed.hostname.replace(/\.+$/, '').toLowerCase()
        if (host === 'pornhub.com' || host.endsWith('.pornhub.com')) {
          key = line.match(phVideoKeyPattern)?.[1] || ''
          valid = Boolean(key)
        } else if (xhHostPattern.test(host)) {
          key = extractXhamsterVideoKey(parsed.pathname)
          valid = Boolean(key)
        }
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
