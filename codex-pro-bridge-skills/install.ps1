[CmdletBinding()]
param(
    [switch]$Global,
    [string]$Repo,
    [switch]$Setup,
    [string]$CodexHome,
    [string]$Python,
    [ValidateSet("auto", "windows-native", "wsl-windows", "native")]
    [string]$Topology = "auto",
    [string]$UiProfile,
    [switch]$NoConnect
)
$ErrorActionPreference = "Stop"
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = if ($Python) { $Python } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
$pythonArgs = if ($launcher -eq "py") { @("-3") } else { @() }
$mode = if ($Setup) { "install" } else { "skills" }
$setupArgs = @((Join-Path $packageRoot "setup_bridge.py"), $mode)
if ($Setup -and -not $Repo) { throw "Full setup requires -Repo to authorize one explicit repository." }
if ($Global -and $Repo -and -not $Setup) { throw "Choose -Global or -Repo for a skills-only install." }
if ($Repo) {
    $setupArgs += @("--repo", $Repo)
    if (-not $Setup) { $setupArgs += "--repo-local" }
}
if ($CodexHome) { $setupArgs += @("--codex-home", $CodexHome) }
if ($Setup) { $setupArgs += @("--topology", $Topology) }
if ($UiProfile) { $setupArgs += @("--ui-profile", $UiProfile) }
if ($NoConnect) { $setupArgs += "--no-connect" }
& $launcher @pythonArgs @setupArgs
exit $LASTEXITCODE
