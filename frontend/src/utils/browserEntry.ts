const entryCookie = 'phdl_entry_seen=1'

// Keep this separate from the persistent login cookie. Browser session markers
// are shared across tabs and normally expire when the browser session ends.
export function markBrowserEntry(): void {
  const secure = window.location.protocol === 'https:' ? '; Secure' : ''
  document.cookie = `${entryCookie}; Path=/; SameSite=Lax${secure}`
}

export function isNewBrowserEntry(): boolean {
  const seen = document.cookie.split(';').some((value) => value.trim() === entryCookie)
  const navigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
  return !seen && navigation?.type !== 'reload' && navigation?.type !== 'back_forward'
}
