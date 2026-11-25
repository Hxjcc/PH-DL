const PROFILE_PATH_RE = /\/(?:model|pornstar|users|user)\/([^/?#]+)/i

export function normalizeTargetUsername(token) {
  const raw = String(token || '').trim()
  if (!raw) return ''
  if (/^https?:\/\//i.test(raw) || raw.includes('pornhub.')) {
    const match = raw.match(PROFILE_PATH_RE)
    if (match?.[1]) {
      try {
        return decodeURIComponent(match[1]).trim()
      } catch {
        return match[1].trim()
      }
    }
  }
  return raw.replace(/^@+/, '').trim()
}

export function parseTargetUsernames(raw) {
  const tokens = String(raw || '')
    .split(/[\s,，;；]+/)
    .map(normalizeTargetUsername)
    .filter(Boolean)
  const seen = new Set()
  const unique = []
  for (const name of tokens) {
    const key = name.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    unique.push(name)
  }
  return unique
}

export function buildTargetProfileVideosUrl(target) {
  const type = String(target?.target_type || 'model').trim().toLowerCase()
  if (type === 'liked') return 'https://www.pornhub.com/likedvideos'
  if (type === 'user' || type === 'users') return ''
  const username = String(target?.username || '').trim()
  if (!username || username === '_currentUser_') return ''
  if (type === 'favorites') {
    return `https://www.pornhub.com/users/${encodeURIComponent(username.toLowerCase())}/videos/favorites`
  }
  const slug = encodeURIComponent(username)
  if (type === 'pornstar') return `https://www.pornhub.com/pornstar/${slug}/videos`
  return `https://www.pornhub.com/${encodeURIComponent(type || 'model')}/${slug}/videos`
}
