# Ouroboros installer for native Windows (PowerShell 5.1+ / pwsh 7+).
#
# Usage (no Python, no uv, no Git needed beforehand):
#   irm https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.ps1 | iex
#
# With options (the pipe form cannot take parameters, so bind the script first):
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.ps1))) -Runtime codex
#
# Environment variables mirror scripts/install.sh and work with the pipe form:
#   OUROBOROS_INSTALL_RUNTIME       claude|claude-sdk|claude-cli|codex|opencode|hermes|gemini|goose|kiro|copilot|pi|gjc|all
#   OUROBOROS_INSTALL_RECONFIGURE   set to 1 to ignore the runtime saved in ~/.ouroboros/config.yaml
#   OUROBOROS_INSTALL_PRE           set to 1 to allow pre-release versions
#
# What it does, in order:
#   1. Git >= 2.36 and uv: installed through winget when missing (uv falls back
#      to the astral.sh installer). uv downloads its own Python, so the machine
#      does not need one.
#   2. Picks a backend the same way install.sh does: explicit > saved config >
#      single detected CLI > prompt.
#   3. `uv tool install ouroboros-ai` with the same pins as install.sh.
#   4. `ouroboros setup --runtime <backend> --non-interactive`, `setup refresh`,
#      and the Claude Code plugin when `claude` is on PATH.
#
# This installer emits no installer events of its own (no install_started /
# install_completed). The `ouroboros` CLI it invokes for setup follows the normal
# telemetry controls in TELEMETRY.md, exactly as it does under install.sh.
# Native Windows support is experimental; Codex CLI is only supported under WSL 2 (docs/platform-support.md).

[CmdletBinding()]
param(
    [string]$Runtime = $env:OUROBOROS_INSTALL_RUNTIME,
    [switch]$Reconfigure,
    [switch]$Pre
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

# NOTE: pin specs MUST mirror scripts/install.sh and [project.optional-dependencies]
# in pyproject.toml. tests/unit/scripts/test_install_ps1.py fails on drift.
$PackageName = 'ouroboros-ai'
$ClickSpec = 'click>=8.1.0,<9.0.0'
$DefaultPythonSpec = '>=3.12'
$LiteLLMPythonSpec = '>=3.12,<3.14'
$MinGitVersion = [version]'2.36.0'
$MinPython = [version]'3.12'
$HookPythonVersion = '3.13'
$AllRuntimes = @('claude', 'claude-sdk', 'claude-cli', 'codex', 'opencode', 'hermes', 'gemini', 'goose', 'kiro', 'copilot', 'pi', 'gjc')

if ([string]::IsNullOrWhiteSpace($Runtime)) { $Runtime = '' }
if (-not $Reconfigure -and $env:OUROBOROS_INSTALL_RECONFIGURE) { $Reconfigure = $true }
if (-not $Pre -and $env:OUROBOROS_INSTALL_PRE) { $Pre = $true }

# --- output helpers -----------------------------------------------------------

function Write-Step([string]$Title, [string]$Detail) {
    Write-Host ''
    Write-Host "== $Title" -ForegroundColor Cyan
    if ($Detail) { Write-Host "   $Detail" -ForegroundColor DarkGray }
}
function Write-Ok([string]$Message) { Write-Host "  [ok] $Message" -ForegroundColor Green }
function Write-Info([string]$Message) { Write-Host "  -  $Message" -ForegroundColor DarkGray }
function Write-Warn([string]$Message) { Write-Host "  [!] $Message" -ForegroundColor Yellow }
function Write-Err([string]$Message) { Write-Host "  [x] $Message" -ForegroundColor Red }

function Test-Interactive {
    try { return -not [Console]::IsInputRedirected } catch { return $false }
}

# Installers register their directories in the user/machine PATH, but the
# current session keeps the PATH it started with. Merge the registry PATH in so
# a tool installed a moment ago is visible without opening a new terminal.
function Sync-SessionPath {
    $parts = New-Object System.Collections.Generic.List[string]
    $seen = @{}
    foreach ($scope in @('Process', 'User', 'Machine')) {
        $value = [Environment]::GetEnvironmentVariable('Path', $scope)
        if (-not $value) { continue }
        foreach ($entry in $value.Split(';')) {
            $trimmed = $entry.Trim()
            if (-not $trimmed) { continue }
            $key = $trimmed.TrimEnd('\').ToLowerInvariant()
            if ($seen.ContainsKey($key)) { continue }
            $seen[$key] = $true
            $parts.Add($trimmed)
        }
    }
    $env:Path = ($parts -join ';')
}

function Add-SessionPathFront([string]$Directory) {
    if (-not $Directory) { return }
    $current = $env:Path.Split(';') | ForEach-Object { $_.TrimEnd('\').ToLowerInvariant() }
    if ($current -contains $Directory.TrimEnd('\').ToLowerInvariant()) { return }
    $env:Path = "$Directory;$env:Path"
}

function Get-CommandPath([string]$Name) {
    $found = Get-Command $Name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    return $null
}

# Native commands do not throw on Windows PowerShell 5.1; check the exit code.
function Invoke-Native([string]$Command, [string[]]$Arguments) {
    $global:LASTEXITCODE = 0
    # Output goes straight to the host so the function returns only the bool.
    & $Command @Arguments | Out-Host
    return ($LASTEXITCODE -eq 0)
}

function Test-Winget {
    return [bool](Get-CommandPath 'winget')
}

function Install-WithWinget([string]$Id, [string]$Label) {
    if (-not (Test-Winget)) { return $false }
    Write-Info "Installing $Label with winget ($Id)..."
    $ok = Invoke-Native 'winget' @('install', '--id', $Id, '-e', '--silent', '--accept-source-agreements', '--accept-package-agreements')
    Sync-SessionPath
    return $ok
}

# --- banner -------------------------------------------------------------------

Write-Host ''
Write-Host '  Ouroboros installer (Windows)' -ForegroundColor Magenta
Write-Host '  Specification-first AI development' -ForegroundColor DarkGray
Write-Host ''
Write-Warn 'Native Windows support is experimental. Codex CLI needs WSL 2; for the fully supported path run scripts/install.sh inside WSL 2.'

# --- 1/4 prerequisites: Git and uv -------------------------------------------

Write-Step '1/4  Checking Git and uv' 'Both are installed automatically when missing. No Python is required.'

Sync-SessionPath

$gitPath = Get-CommandPath 'git'
if (-not $gitPath) {
    Write-Warn 'Git not found.'
    if (-not (Install-WithWinget 'Git.Git' 'Git')) {
        Write-Err 'Git >= 2.36 is required and could not be installed automatically.'
        Write-Info 'Install it from https://git-scm.com/download/win, open a new PowerShell window, and run this installer again.'
        exit 1
    }
    $gitPath = Get-CommandPath 'git'
    if (-not $gitPath) {
        Write-Err 'Git was installed but is not on PATH yet. Open a new PowerShell window and run this installer again.'
        exit 1
    }
}
$gitVersionText = (& git --version 2>$null) -join ' '
$gitMatch = [regex]::Match($gitVersionText, '(\d+)\.(\d+)\.(\d+)')
if ($gitMatch.Success) {
    $gitVersion = [version]("{0}.{1}.{2}" -f $gitMatch.Groups[1].Value, $gitMatch.Groups[2].Value, $gitMatch.Groups[3].Value)
    if ($gitVersion -lt $MinGitVersion) {
        Write-Err "Git $gitVersion found, but Ouroboros needs Git >= $MinGitVersion."
        Write-Info 'Upgrade with: winget upgrade --id Git.Git -e'
        exit 1
    }
    Write-Ok "Git found: $gitVersionText"
} else {
    Write-Warn "Could not parse the Git version from '$gitVersionText'; continuing."
}

$uvPath = Get-CommandPath 'uv'
if (-not $uvPath) {
    Write-Warn 'uv not found.'
    if (-not (Install-WithWinget 'astral-sh.uv' 'uv')) {
        Write-Info 'winget unavailable or failed; using the astral.sh installer...'
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $uvInstaller = Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing
            Invoke-Expression $uvInstaller
        } catch {
            Write-Err "uv could not be installed: $($_.Exception.Message)"
            Write-Info 'Install it manually: winget install --id astral-sh.uv -e   (or see https://docs.astral.sh/uv/getting-started/installation/)'
            exit 1
        }
        Sync-SessionPath
    }
    $uvPath = Get-CommandPath 'uv'
    if (-not $uvPath) {
        $fallbackBin = Join-Path $env:USERPROFILE '.local\bin'
        if (Test-Path (Join-Path $fallbackBin 'uv.exe')) {
            Add-SessionPathFront $fallbackBin
            $uvPath = Get-CommandPath 'uv'
        }
    }
    if (-not $uvPath) {
        Write-Err 'uv was installed but is not on PATH yet. Open a new PowerShell window and run this installer again.'
        exit 1
    }
}
Write-Ok "uv found: $((& uv --version 2>$null) -join ' ')"

# --- 2/4 backend selection ------------------------------------------------------

Write-Step '2/4  Choosing an agent backend' 'Claude, Codex, Hermes, OpenCode, Gemini, Goose, Kiro, Copilot, Pi, and GJC are supported.'

$detected = @{}
foreach ($pair in @(
        @('claude', 'claude'), @('codex', 'codex'), @('hermes', 'hermes'), @('opencode', 'opencode'),
        @('gemini', 'gemini'), @('goose', 'goose'), @('kiro', 'kiro-cli'), @('copilot', 'copilot'),
        @('pi', 'pi'), @('gjc', 'gjc'))) {
    $key = $pair[0]
    $exe = $pair[1]
    $found = Get-CommandPath $exe
    if ($found) {
        $detected[$key] = $found
        Write-Ok "$key found: $found"
    }
}

# Read the previously configured runtime so upgrades keep the user's choice.
function Get-SavedRuntime {
    $config = Join-Path $env:USERPROFILE '.ouroboros\config.yaml'
    if (-not (Test-Path $config)) { return '' }
    $supported = @('claude', 'claude_mcp', 'codex', 'opencode', 'hermes', 'gemini', 'goose', 'kiro', 'copilot', 'pi', 'gjc')
    $inOrchestrator = $false
    foreach ($line in (Get-Content $config -ErrorAction SilentlyContinue)) {
        if ($line -match '^orchestrator:\s*(#.*)?$') { $inOrchestrator = $true; continue }
        if ($inOrchestrator -and $line -and -not [char]::IsWhiteSpace($line[0])) { break }
        if ($inOrchestrator) {
            $m = [regex]::Match($line, '^\s+runtime_backend:\s*["'']?([^"''\s#]+)')
            if ($m.Success -and ($supported -contains $m.Groups[1].Value)) {
                # claude is the SDK/MCP 1 profile; claude_mcp is the CLI/MCP 2 worker profile.
                if ($m.Groups[1].Value -eq 'claude_mcp') { return 'claude-cli' }
                return $m.Groups[1].Value
            }
        }
    }
    return ''
}

function Read-Choice([string]$Prompt, [string]$Default) {
    $answer = Read-Host $Prompt
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim()
}

function Select-RuntimeInteractive([bool]$AllowNone) {
    $menu = @(
        @('1', 'claude', "Claude SDK (MCP 1.x) + skills; MCP 2 server is isolated ($PackageName[claude,tui])"),
        @('2', 'codex', "Codex plugin artifacts ($PackageName[tui]) -- native Windows: WSL 2 recommended"),
        @('3', 'hermes', "Hermes agent guides + MCP server ($PackageName[mcp,tui])"),
        @('4', 'opencode', "OpenCode commands and agent files ($PackageName[tui])"),
        @('5', 'gemini', "Gemini CLI integration ($PackageName[tui])"),
        @('6', 'goose', "Goose CLI integration ($PackageName[tui])"),
        @('7', 'kiro', "Kiro CLI integration ($PackageName[tui])"),
        @('8', 'copilot', "GitHub Copilot integration ($PackageName[tui])"),
        @('9', 'pi', "Pi CLI bridge and instruction artifacts ($PackageName[tui])"),
        @('10', 'gjc', "GJC CLI bridge and instruction artifacts ($PackageName[tui])"),
        @('11', 'all', "Install every optional integration ($PackageName[all])")
    )
    foreach ($item in $menu) { Write-Host ("  {0,2}) {1,-9} {2}" -f $item[0], $item[1], $item[2]) }
    if ($AllowNone) { Write-Host ("  {0,2}) {1,-9} {2}" -f '0', 'none', 'Base CLI only; choose a backend later') }
    $choice = Read-Choice 'Select [1]' '1'
    if ($AllowNone -and $choice -eq '0') { return '' }
    foreach ($item in $menu) { if ($item[0] -eq $choice) { return $item[1] } }
    return 'claude'
}

$selected = ''
if ($Runtime) {
    if ($AllRuntimes -notcontains $Runtime -and $Runtime -ne 'all') {
        Write-Err "unsupported runtime '$Runtime'"
        Write-Info "Expected one of: $($AllRuntimes -join ', '), all"
        exit 1
    }
    $selected = $Runtime
    Write-Ok "Runtime: $selected (from -Runtime / OUROBOROS_INSTALL_RUNTIME)"
} else {
    $saved = ''
    if (-not $Reconfigure) { $saved = Get-SavedRuntime }
    if ($saved) {
        $selected = $saved
        Write-Ok "Runtime: $selected (preserved from ~/.ouroboros/config.yaml)"
        Write-Info 'Re-run with -Reconfigure (or OUROBOROS_INSTALL_RECONFIGURE=1) to choose again.'
    } elseif ($detected.Count -gt 1) {
        if (Test-Interactive) {
            Write-Host ''
            Write-Host 'Multiple runtimes detected. Pick where Ouroboros should appear first:'
            $selected = Select-RuntimeInteractive $false
        } else {
            Write-Warn 'Multiple runtimes detected in non-interactive mode; defaulting to Claude.'
            $selected = 'claude'
        }
    } elseif ($detected.Count -eq 1) {
        $selected = @($detected.Keys)[0]
    } else {
        if (Test-Interactive) {
            Write-Host ''
            Write-Host 'No runtime CLI detected yet. Choose the agent you plan to use:'
            $selected = Select-RuntimeInteractive $true
        } else {
            Write-Warn 'No runtime detected in non-interactive mode; installing the base package.'
            Write-Info "Pick a backend afterwards with: ouroboros setup --runtime <$($AllRuntimes -join '|')>"
            $selected = ''
        }
    }
}

if ($selected -eq 'codex') {
    Write-Warn 'Codex CLI is not supported on native Windows. Setup will not register a persistent MCP server here; use WSL 2 for Codex.'
}

# Map the backend to the uv --with pins. `tui` ships with every selection so
# `ouroboros config` works out of the box; `[all]` stays on the MCP 1.x bundle.
$extraPins = @()
$extrasLabel = '[tui]'
$setupRuntime = $selected
$pythonSpec = $DefaultPythonSpec
switch ($selected) {
    'claude' { $extrasLabel = '[claude,tui]'; $extraPins = @('claude-agent-sdk==0.2.144', 'anthropic==0.122.0', 'mcp==1.28.1') }
    'claude-sdk' { $extrasLabel = '[claude-sdk,tui]'; $extraPins = @('claude-agent-sdk==0.2.144', 'anthropic==0.122.0', 'mcp==1.28.1') }
    'claude-cli' { $extrasLabel = '[claude-cli,tui]' }
    'hermes' { $extrasLabel = '[mcp,tui]'; $extraPins = @('mcp==2.0.0') }
    'all' {
        $extrasLabel = '[all]'
        $extraPins = @('claude-agent-sdk==0.2.144', 'anthropic==0.122.0', 'mcp==1.28.1', 'litellm==1.91.0')
        $pythonSpec = $LiteLLMPythonSpec
        $setupRuntime = ''
    }
    default { }
}
if ($setupRuntime) { Write-Ok "Selected backend: $setupRuntime" }
elseif ($selected -eq 'all') { Write-Ok 'Selected backend: all' }
else { Write-Info 'Selected backend: none yet' }

# --- 3/4 install ------------------------------------------------------------------

Write-Step '3/4  Installing Ouroboros' "Package: $PackageName$extrasLabel via uv tool install (uv downloads Python $pythonSpec itself)"

$uvArgs = @('tool', 'install', '--upgrade', '--python', $pythonSpec, $PackageName, '--with', $ClickSpec)
if ($Pre) { $uvArgs += '--prerelease=allow' }
foreach ($pin in $extraPins) { $uvArgs += @('--with', $pin) }
$uvArgs += @('--with', 'textual==8.2.8', '--with', 'textual-serve==1.1.3')

Write-Info "Running: uv $($uvArgs -join ' ')"
if (-not (Invoke-Native 'uv' $uvArgs)) {
    Write-Err 'uv tool install failed. Fix the error above and run the installer again.'
    exit 1
}

# Put the tool bin directory on PATH: persistently (uv edits the user PATH) and
# for this session, so setup below runs the binary that was just installed.
$toolBin = ((& uv tool dir --bin 2>$null) -join '').Trim()
if (-not (Invoke-Native 'uv' @('tool', 'update-shell'))) {
    Write-Warn "Could not update the user PATH automatically. Add this directory to PATH: $toolBin"
}
if ($toolBin) { Add-SessionPathFront $toolBin }
$ouroborosExe = $null
if ($toolBin -and (Test-Path (Join-Path $toolBin 'ouroboros.exe'))) {
    $ouroborosExe = Join-Path $toolBin 'ouroboros.exe'
} else {
    $ouroborosExe = Get-CommandPath 'ouroboros'
}
if (-not $ouroborosExe) {
    Write-Err "Ouroboros was installed but 'ouroboros' is not on PATH. Add $toolBin to PATH, open a new window, and run: ouroboros setup --runtime <backend>"
    exit 1
}
Write-Ok "Installed: $((& $ouroborosExe --version 2>$null) -join ' ')"

# Optional: the Claude Code plugin hooks look for python3/python on PATH and
# skip themselves when none is found. Skills fall back to uv on their own, so
# this is a convenience, not a requirement, and its failure is not fatal.
function Test-PythonAtLeast([string]$Exe) {
    $found = Get-CommandPath $Exe
    if (-not $found) { return $false }
    # The Microsoft Store alias python.exe/python3.exe prints a hint and fails
    # here, so it counts as absent instead of as an interpreter.
    $out = (& $found -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null) -join ''
    if ($LASTEXITCODE -ne 0 -or -not ($out -match '^\d+\.\d+$')) { return $false }
    return ([version]$out -ge $MinPython)
}
if ((Test-PythonAtLeast 'python3') -or (Test-PythonAtLeast 'python')) {
    Write-Ok "Python >= $MinPython found on PATH (used by the Claude Code plugin hooks)"
} else {
    Write-Info "No Python >= $MinPython on PATH; installing a uv-managed Python $HookPythonVersion for the plugin hooks..."
    if (Invoke-Native 'uv' @('python', 'install', '--default', $HookPythonVersion)) {
        Write-Ok "Python $HookPythonVersion installed (python/python3 in $((& uv python dir --bin 2>$null) -join ''))"
        Write-Info 'If `python` still opens the Microsoft Store, turn off its App execution alias in Settings > Apps > Advanced app settings.'
    } else {
        Write-Warn 'Could not install a default Python; plugin hooks will skip themselves until one is on PATH. Skills keep working through uv.'
    }
}

# --- 4/4 setup ------------------------------------------------------------------

Write-Step '4/4  Wiring local integrations' 'Creates config and runtime-specific files when a backend was selected.'
if ($setupRuntime) {
    Write-Info "Running: ouroboros setup --runtime $setupRuntime --non-interactive"
    if (-not (Invoke-Native $ouroborosExe @('setup', '--runtime', $setupRuntime, '--non-interactive'))) {
        Write-Warn 'Runtime setup failed; installation is incomplete. Fix the error above and re-run setup.'
        exit $LASTEXITCODE
    }
} else {
    Write-Info 'No backend selected; skipping runtime setup.'
}

# Refresh artifacts for every detected runtime (presence-gated, never touches
# MCP registrations or config), so upgrades do not leave other hosts stale.
Write-Info 'Refreshing runtime artifacts for detected runtimes'
if (-not (Invoke-Native $ouroborosExe @('setup', 'refresh'))) {
    Write-Warn 'Artifact refresh skipped; run: ouroboros setup refresh'
}

if ($detected.ContainsKey('claude')) {
    Write-Host ''
    Write-Host '  Claude Code skills' -ForegroundColor Blue
    Write-Info 'Installing Ouroboros skills via Claude plugin marketplace...'
    Invoke-Native 'claude' @('plugin', 'marketplace', 'add', 'Q00/ouroboros') | Out-Null
    Invoke-Native 'claude' @('plugin', 'marketplace', 'update', 'ouroboros') | Out-Null
    if (Invoke-Native 'claude' @('plugin', 'install', 'ouroboros@ouroboros')) {
        # `install` is a no-op for an already-installed plugin; `update` moves it to the latest version.
        Invoke-Native 'claude' @('plugin', 'update', 'ouroboros@ouroboros') | Out-Null
        Write-Ok 'Skills installed'
    } else {
        Write-Warn 'Skills skipped. Manual install: claude plugin marketplace add Q00/ouroboros; claude plugin install ouroboros@ouroboros'
    }
}

Write-Host ''
Write-Host 'Done! Ouroboros is ready.' -ForegroundColor Green
Write-Host ''
Write-Host 'Get started'
Write-Info 'Open a NEW PowerShell window (PATH changes apply there), then in your AI coding agent run: > ooo interview "your idea here"'
Write-Info 'Or from the terminal: ouroboros init start "your idea here"'
if ($setupRuntime) { Write-Info "Current backend: $setupRuntime" }
Write-Info "Switch backend later: ouroboros setup --runtime <$($AllRuntimes -join '|')>"
Write-Host 'Model settings'
Write-Info 'Inside your AI agent: > ooo config   (opens in your browser)'
Write-Info 'From this terminal:  ouroboros config   (full-screen TUI; use Windows Terminal, not cmd.exe)'
Write-Host ''
Write-Host 'Like Ouroboros? Star the repo: https://github.com/Q00/ouroboros'
