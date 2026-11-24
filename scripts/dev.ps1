param(
  [Parameter(Mandatory, Position = 0)]
  [ValidateSet('backend', 'frontend')]
  [string]$Service,

  [switch]$Reload
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

Push-Location -LiteralPath (Join-Path $projectRoot $Service)
try {
  if ($Service -eq 'backend') {
    $python = Join-Path $PWD '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
      throw 'Backend environment is missing. Follow docs/development.md to install it.'
    }
    $serverArgs = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000')
    if ($Reload) { $serverArgs += '--reload' }
    & $python @serverArgs
  } else {
    if ($Reload) {
      throw 'The frontend already reloads on change. Use -Reload only with the backend.'
    }
    if (-not (Test-Path -LiteralPath 'node_modules' -PathType Container)) {
      throw 'Frontend dependencies are missing. Run npm ci from frontend/.'
    }
    npm.cmd run dev -- --host 127.0.0.1 --strictPort
  }
  $serverExitCode = $LASTEXITCODE
} finally {
  Pop-Location
}

exit $serverExitCode
