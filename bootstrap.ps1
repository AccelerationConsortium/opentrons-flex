# One-step start for Windows (PowerShell): installs uv if needed, installs the
# connector and its tools, then runs the guided setup (simulator or real Flex).
#
#   powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
#   powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1 --host 10.0.0.42
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (Python package manager)..."
    powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Write-Warning "OpenSSH client is needed to deploy to a robot: Settings > Apps > Optional features > OpenSSH Client."
}
$gitSh = Join-Path $env:ProgramFiles "Git\bin\sh.exe"
if (-not (Get-Command sh -ErrorAction SilentlyContinue) -and -not (Test-Path $gitSh)) {
    Write-Warning "Git for Windows (https://git-scm.com/download/win) is needed to deploy to a robot. The simulator works without it."
}

Write-Host "Installing the connector and flexctl (Python 3.12)..."
uv sync --python 3.12 --all-extras
uv run flexctl setup @args
exit $LASTEXITCODE
