[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SecuredChatRoot,

    [Parameter(Mandatory = $true)]
    [string]$BusRoot,

    [string]$Room = "prometheus-relay",

    [ValidateSet("off", "warn", "strict")]
    [string]$VerifySig = "warn",

    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,

    [switch]$Check
)

$ErrorActionPreference = "Stop"
$bridge = Join-Path $PSScriptRoot "companion\bridge.py"
$chatPy = Join-Path $SecuredChatRoot "cli\chat.py"

if (-not (Test-Path -LiteralPath $bridge -PathType Leaf)) {
    throw "Companion bridge not found: $bridge"
}
if (-not (Test-Path -LiteralPath $chatPy -PathType Leaf)) {
    throw "SecuredChat CLI not found: $chatPy"
}
if (-not (Test-Path -LiteralPath $BusRoot -PathType Container)) {
    throw "SecuredChat bus directory not found: $BusRoot"
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python was not found on PATH. Install Python 3.10+ or add python.exe to PATH."
}

$arguments = @(
    $bridge,
    "--chat-py", $chatPy,
    "--bus", $BusRoot,
    "--room", $Room,
    "--verify-sig", $VerifySig,
    "--port", $Port
)
if ($Check) {
    $arguments += "--check"
}

& python @arguments
exit $LASTEXITCODE

