export function normalizePositionPart(part) {
  const m = String(part || '').trim().match(/^(\d{1,5})(?:\s*-\s*(\d{1,5}))?$/)
  if (!m) return null
  const start = Number(m[1])
  const end = m[2] ? Number(m[2]) : start
  if (start < 1 || end < start || end > 10000) return null
  return start === end ? String(start) : `${start}-${end}`
}

export function parseSpecificVideoDraft(raw, currentTags = [], defaultPage = 1) {
  const safeDefaultPage = Math.max(1, Number(defaultPage) || 1)
  const nextTags = [...currentTags]
  const segments = String(raw || '')
    .split(/[;；]+/)
    .map((s) => s.trim())
    .filter(Boolean)
  let invalidFound = false

  for (const seg of segments) {
    let page = safeDefaultPage
    let posExpr = seg
    const colonMatch = seg.match(/^[pP第]?(\d{1,5})页?\s*[:：]\s*(.+)$/)
    const dashMultiMatch = !colonMatch && segments.length > 1 ? seg.match(/^(\d{1,5})\s*-\s*(.+)$/) : null
    if (colonMatch) {
      page = Number(colonMatch[1])
      posExpr = colonMatch[2]
    } else if (dashMultiMatch) {
      page = Number(dashMultiMatch[1])
      posExpr = dashMultiMatch[2]
    }
    if (page < 1 || page > 10000) {
      invalidFound = true
      continue
    }
    const rawParts = posExpr
      .split(/[,，\s]+/)
      .map((p) => p.trim())
      .filter(Boolean)
    const normalizedParts = []
    for (const rp of rawParts) {
      const norm = normalizePositionPart(rp)
      if (!norm) {
        invalidFound = true
      } else {
        normalizedParts.push(norm)
      }
    }
    if (normalizedParts.length > 0) {
      nextTags.push(`${page}页:${normalizedParts.join(',')}`)
    }
  }

  return { tags: nextTags, invalidFound }
}

export function serializeSpecificVideoSpecs(tags = [], fallbackPage = 1) {
  const byPage = new Map()
  const safeFallbackPage = Math.max(1, Number(fallbackPage) || 1)
  for (const rawTag of tags) {
    const text = String(rawTag || '').trim()
    if (!text) continue
    const m = text.match(/^(\d{1,5})页:(.+)$/)
    const page = m ? Number(m[1]) : safeFallbackPage
    const expr = m ? m[2] : text
    const parts = expr.split(',').map(normalizePositionPart).filter(Boolean)
    if (!parts.length) continue
    if (!byPage.has(page)) byPage.set(page, [])
    const existing = byPage.get(page)
    for (const p of parts) {
      if (!existing.includes(p)) existing.push(p)
    }
  }
  return Array.from(byPage.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([page, parts]) => `${page}-${parts.join(',')}`)
    .join(';')
}
