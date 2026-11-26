param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [Parameter(Mandatory = $true)][string]$CredentialDirectory,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$ServerPort
)

$ErrorActionPreference = 'Stop'
try {
    $config = Get-Content -LiteralPath (Join-Path $CredentialDirectory 'client.json') -Raw | ConvertFrom-Json
    if ($config.agent -notin @('pilot:local', 'pilot:remote')) { throw 'Invalid synthetic pilot agent' }
    $env:HARDLINE_LAN_TEST_HOST = [string]$config.server_ip
    $env:HARDLINE_LAN_TEST_PORT = [string]$ServerPort
    $env:HARDLINE_LAN_TEST_CA = Join-Path $CredentialDirectory 'ca.crt'
    $env:HARDLINE_LAN_TEST_CERT = Join-Path $CredentialDirectory 'identity.crt'
    $env:HARDLINE_LAN_TEST_KEY = Join-Path $CredentialDirectory 'identity.key'
    $env:HARDLINE_LAN_TEST_TOKEN = (Get-Content -LiteralPath (Join-Path $CredentialDirectory 'agent.token') -Raw).Trim()
    Set-Location -LiteralPath $RepoRoot
    & $PythonPath -m experiments.lan_tls_client
    exit $LASTEXITCODE
}
catch {
    # stderr is separate from the MCP stdio protocol. Never print exceptions,
    # environment values, certificate contents or token contents.
    [Console]::Error.WriteLine('Synthetic LAN pilot client could not start; check private files and environment.')
    exit 1
}
