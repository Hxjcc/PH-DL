export const JOB_LIMITS = [30, 50, 100, 200]
export const HISTORY_PAGE_SIZES = [10, 30, 50, 100]

const storageKeys = {
  jobs: 'phdl-jobs-limit',
  history: 'phdl-history-page-size',
}

export function readListPreference(kind, choices, fallback = 30) {
  try {
    const value = Number(localStorage.getItem(storageKeys[kind]))
    return choices.includes(value) ? value : fallback
  } catch {
    return fallback
  }
}

export function saveListPreference(kind, value) {
  try {
    localStorage.setItem(storageKeys[kind], String(value))
  } catch {}
}
