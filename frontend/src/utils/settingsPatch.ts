import type { SelectorSettings, SettingsInput, SettingsPatch } from '../types/api'

const NON_EDITABLE = new Set(['has_cookie', 'has_xh_cookie', 'account_avatar_version', 'rclone_bin'])

export function settingsPatch(
  draft: SettingsInput,
  baseline: SettingsInput,
  includes: (key: string) => boolean = () => true,
): SettingsPatch {
  const patch: SettingsPatch = Object.fromEntries(
    Object.entries(draft).filter(([key, value]) =>
      key !== 'selectors' && !NON_EDITABLE.has(key) && includes(key) && value !== baseline[key as keyof SettingsInput]
    ),
  )
  if (includes('selectors')) {
    const changed = Object.entries(draft.selectors).filter(([key, value]) =>
      value !== baseline.selectors[key as keyof SelectorSettings]
    )
    if (changed.length) patch.selectors = Object.fromEntries(changed)
  }
  return patch
}
