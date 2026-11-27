export const JOB_LIMITS = [10, 30, 50, 100, 200]
export const HISTORY_PAGE_SIZES = [10, 30, 50, 100]
export const TARGETS_PAGE_SIZES = [12, 24, 36, 60, 0]

const storageKeys = {
  jobs: 'phdl-jobs-limit',
  history: 'phdl-history-page-size',
  targets: 'phdl-targets-page-size',
}

export function readListPreference(kind: keyof typeof storageKeys, choices: readonly number[], fallback = 30): number {
  try {
    const raw = localStorage.getItem(storageKeys[kind])
    if (raw === null || raw === '') return fallback
    const value = Number(raw)
    return choices.includes(value) ? value : fallback
  } catch {
    return fallback
  }
}

export function saveListPreference(kind: keyof typeof storageKeys, value: number): void {
  try {
    localStorage.setItem(storageKeys[kind], String(value))
  } catch {}
}
