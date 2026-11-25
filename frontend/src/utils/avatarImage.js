const MAX_SOURCE_BYTES = 10 * 1024 * 1024
const MAX_GIF_PASSTHROUGH_BYTES = 1.5 * 1024 * 1024
const AVATAR_SIZE = 256

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error('无法读取所选图片，请重试'))
    reader.readAsDataURL(file)
  })
}

export async function prepareAvatarDataUrl(file) {
  if (!file) throw new Error('请选择图片文件')
  if (!String(file.type || '').startsWith('image/')) {
    throw new Error('请选择 PNG、JPG、WebP 或 GIF 图片')
  }
  if (file.size > MAX_SOURCE_BYTES) {
    throw new Error('所选图片超过 10 MB，请换一张小一点的图片')
  }
  if (file.type === 'image/gif' && file.size <= MAX_GIF_PASSTHROUGH_BYTES) {
    return readAsDataUrl(file)
  }

  const objectUrl = URL.createObjectURL(file)
  try {
    const img = await new Promise((resolve, reject) => {
      const el = new Image()
      el.onload = () => resolve(el)
      el.onerror = () => reject(new Error('图片解码失败，请换一张 PNG/JPG/WebP 图片'))
      el.src = objectUrl
    })
    const w = img.naturalWidth || img.width
    const h = img.naturalHeight || img.height
    if (!w || !h) throw new Error('图片尺寸无效')

    const side = Math.min(w, h)
    const sx = Math.floor((w - side) / 2)
    const sy = Math.floor((h - side) / 2)

    const canvas = document.createElement('canvas')
    canvas.width = AVATAR_SIZE
    canvas.height = AVATAR_SIZE
    const ctx = canvas.getContext('2d')
    if (!ctx) return readAsDataUrl(file)
    ctx.imageSmoothingEnabled = true
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(img, sx, sy, side, side, 0, 0, AVATAR_SIZE, AVATAR_SIZE)

    const webp = canvas.toDataURL('image/webp', 0.9)
    if (webp.startsWith('data:image/webp')) return webp
    return canvas.toDataURL('image/png')
  } finally {
    URL.revokeObjectURL(objectUrl)
  }
}
