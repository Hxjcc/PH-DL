import type { RuntimeSettings } from '../types/api'

const REDACTED = '********'
const SECRET_FLAG = /--[\w-]*(?:pass|token|header|secret|credential|key|cookie|auth)[\w-]*/i

function maskRiskyValue(value = '') {
  return /[@?#]/.test(value) || SECRET_FLAG.test(value) ? REDACTED : value
}

export function redactedSettingsFields(settings: RuntimeSettings) {
  return {
    ph_password: settings.ph_password ? REDACTED : '',
    ph_cookie: settings.ph_cookie ? REDACTED : '',
    xh_password: settings.xh_password ? REDACTED : '',
    xh_cookie: settings.xh_cookie ? REDACTED : '',
    serverchan_send_key: settings.serverchan_send_key ? REDACTED : '',
    yescaptcha_client_key: settings.yescaptcha_client_key ? REDACTED : '',
    proxy_url: maskRiskyValue(settings.proxy_url),
    serverchan_api_url: maskRiskyValue(settings.serverchan_api_url),
    rclone_extra_flags: maskRiskyValue(settings.rclone_extra_flags),
  }
}
