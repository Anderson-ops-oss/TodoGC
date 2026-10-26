# Install a persistent, per-user TodoGC command. Run with PowerShell on Windows.
[CmdletBinding()]
param(
    [ValidateSet('codex', 'claude-code', 'all')][string]$Target = 'all',
    [ValidateNotNullOrEmpty()][string]$Source = 'todogc>=0.14.0',
    [string]$Database,
    [string]$Config,
    [switch]$Offline
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'Use install.sh on macOS/Linux; install.ps1 requires Windows.' }
if ($Source.StartsWith('-')) { throw 'Source must be a package specification or path, not an option.' }
if ([string]::IsNullOrEmpty($env:USERPROFILE)) { throw 'USERPROFILE is required.' }

function Test-PersistableDirectory([string]$Directory) {
    if ($Directory.Contains(';') -or $Directory.Contains('%') -or $Directory.Contains("`r") -or $Directory.Contains("`n")) {
        throw "User PATH cannot safely represent this directory: $Directory. Choose a persistent tool directory without semicolons, percent signs, or newlines."
    }
}
function Invoke-Manager([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Executable $($Arguments -join ' ')" }
}
function Get-ManagerOutput([string]$Executable, [string[]]$Arguments) {
    $result = @(Invoke-Manager $Executable $Arguments)
    if ($result.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$result[0])) {
        throw "Tool manager returned an invalid path: $Executable $($Arguments -join ' ')"
    }
    $value = ([string]$result[0]).Trim()
    if (-not [IO.Path]::IsPathRooted($value)) { throw "Tool manager returned a relative path: $value" }
    return $value
}
function Find-Application([string]$Name, [string]$Fallback) {
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) { return $command.Source }
    if (Test-Path -LiteralPath $Fallback -PathType Leaf) { return $Fallback }
    return $null
}
function Same-File([string]$Left, [string]$Right) {
    if (-not (Test-Path -LiteralPath $Left -PathType Leaf) -or
        -not (Test-Path -LiteralPath $Right -PathType Leaf)) { return $false }
    # uv copies launcher executables on Windows; pipx can use either copies or links.
    return (Get-FileHash -LiteralPath $Left).Hash -eq (Get-FileHash -LiteralPath $Right).Hash
}
function Add-UserPath([string]$Directory) {
    # Never persist the current process PATH: it can contain agent/venv directories.
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $entries = @($userPath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $exists = @($entries | Where-Object {
        [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -ieq $Directory.TrimEnd('\')
    }).Count -gt 0
    if (-not $exists) {
        $updated = if ([string]::IsNullOrEmpty($userPath)) { $Directory }
            elseif ($userPath.EndsWith(';')) { $userPath + $Directory }
            else { $userPath + ';' + $Directory }
        [Environment]::SetEnvironmentVariable('Path', $updated, 'User')
    }
}

# Restore the caller's process environment even if installation fails. The only
# persistent PATH change is the specific tool directory in the User registry value.
Test-PersistableDirectory $env:USERPROFILE
$savedEnvironment = @{}
foreach ($name in @('PATH', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'PYTHONHOME', 'PYTHONPATH', 'UV_OFFLINE', 'PIP_NO_INDEX')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $venvBin = if ($env:VIRTUAL_ENV) { Join-Path $env:VIRTUAL_ENV 'Scripts' } else { '' }
    $condaBin = if ($env:CONDA_PREFIX) { Join-Path $env:CONDA_PREFIX 'Scripts' } else { '' }
    $condaRoot = if ($env:CONDA_PREFIX) { $env:CONDA_PREFIX } else { '' }
    $env:PATH = (@($env:PATH -split ';' | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_) -and
        $_.TrimEnd('\') -ine $venvBin.TrimEnd('\') -and
        $_.TrimEnd('\') -ine $condaBin.TrimEnd('\') -and
        $_.TrimEnd('\') -ine $condaRoot.TrimEnd('\')
    }) -join ';')
    foreach ($name in @('VIRTUAL_ENV', 'CONDA_PREFIX', 'PYTHONHOME', 'PYTHONPATH')) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    $uv = Find-Application 'uv' (Join-Path $env:USERPROFILE '.local\bin\uv.exe')
    $pipx = Find-Application 'pipx' (Join-Path $env:USERPROFILE '.local\bin\pipx.exe')
    $uvRoot = $null
    $uvBin = $null
    $pipxRoot = $null
    $pipxBin = $null
    $uvInstalled = $false
    $pipxInstalled = $false
    if ($uv) {
        $uvRoot = Get-ManagerOutput $uv @('tool', 'dir')
        $uvBin = Get-ManagerOutput $uv @('tool', 'dir', '--bin')
        $uvInstalled = Test-Path -LiteralPath (Join-Path $uvRoot 'todogc\uv-receipt.toml') -PathType Leaf
    }
    if ($pipx) {
        $pipxRoot = Get-ManagerOutput $pipx @('environment', '--value', 'PIPX_HOME')
        $pipxBin = Get-ManagerOutput $pipx @('environment', '--value', 'PIPX_BIN_DIR')
        $pipxInstalled = Test-Path -LiteralPath (Join-Path $pipxRoot 'venvs\todogc\pipx_metadata.json') -PathType Leaf
    }
    if ($uvInstalled -and $pipxInstalled) {
        throw 'Both uv and pipx manage TodoGC. Keep one installation before retrying; no files were replaced.'
    }
    $manager = if ($pipxInstalled) { 'pipx' } else { 'uv' }
    $existing = Get-Command todogc -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $existing -and -not ($uvInstalled -or $pipxInstalled)) {
        throw "Another todogc owns the command: $($existing.Definition). Resolve that installation before retrying."
    }
    if ($manager -eq 'uv' -and -not $uv) {
        if ($Offline) { throw 'Offline installation needs an existing uv or pipx TodoGC installation.' }
        $download = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName() + '.ps1')
        try {
            # Download a fixed official HTTPS resource and execute the file, never Source as code.
            Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -OutFile $download -UseBasicParsing
            $oldInstallDir = $env:UV_INSTALL_DIR
            try {
                $env:UV_INSTALL_DIR = Join-Path $env:USERPROFILE '.local\bin'
                & $download
            } finally {
                [Environment]::SetEnvironmentVariable('UV_INSTALL_DIR', $oldInstallDir, 'Process')
            }
        } finally {
            Remove-Item -LiteralPath $download -ErrorAction SilentlyContinue
        }
        $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
        if (-not (Test-Path -LiteralPath $uv -PathType Leaf)) { throw "uv installer did not create $uv" }
        $uvRoot = Get-ManagerOutput $uv @('tool', 'dir')
        $uvBin = Get-ManagerOutput $uv @('tool', 'dir', '--bin')
    }
    if ($manager -eq 'uv') {
        $binDir = $uvBin
        $internalCli = Join-Path $uvRoot 'todogc\Scripts\todogc.exe'
        $installed = $uvInstalled
    } else {
        $binDir = $pipxBin
        $internalCli = Join-Path $pipxRoot 'venvs\todogc\Scripts\todogc.exe'
        $installed = $pipxInstalled
    }
    Test-PersistableDirectory $binDir
    $cli = Join-Path $binDir 'todogc.exe'
    if ((Test-Path -LiteralPath $cli) -and (-not $installed -or -not (Same-File $cli $internalCli))) {
        throw "Another command owns $cli. Resolve that installation before retrying."
    }
    if ($null -ne $existing -and
        ($existing.CommandType -ne 'Application' -or -not (Same-File $existing.Source $internalCli))) {
        throw "Another todogc shadows the user command: $($existing.Definition). Remove that PATH conflict before retrying."
    }
    # A future normal terminal inherits Machine + User PATH, rather than the agent's PATH.
    $futurePath = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    foreach ($directory in @($futurePath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        $expanded = [Environment]::ExpandEnvironmentVariables($directory)
        foreach ($name in @('todogc.exe', 'todogc.cmd', 'todogc.bat', 'todogc.ps1')) {
            $candidate = Join-Path $expanded $name
            if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and
                (-not $installed -or -not (Same-File $candidate $internalCli))) {
                throw "Persistent PATH contains another TodoGC command: $candidate. Resolve the conflict before retrying."
            }
        }
    }
    if ($Offline) { $env:UV_OFFLINE = '1'; $env:PIP_NO_INDEX = '1' }
    $explicitSource = $PSBoundParameters.ContainsKey('Source')
    if ($manager -eq 'uv') {
        if ($installed -and -not $explicitSource) {
            Invoke-Manager $uv @('tool', 'upgrade', 'todogc')
        } elseif (-not $installed) {
            Invoke-Manager $uv @('tool', 'install', '--python', '>=3.10', '--upgrade', '--', $Source)
        } else {
            Invoke-Manager $uv @('tool', 'install', '--upgrade', '--', $Source)
        }
    } elseif ($explicitSource) {
        Invoke-Manager $pipx @('install', '--upgrade', '--', $Source)
    } else {
        Invoke-Manager $pipx @('upgrade', 'todogc')
    }
    if (-not (Same-File $cli $internalCli)) { throw "The manager did not expose its TodoGC command at $cli" }
    $globalArgs = @()
    if ($Database) { $globalArgs += @('--db', $Database) }
    if ($Config) { $globalArgs += @('--config', $Config) }
    Invoke-Manager $cli ($globalArgs + @('setup', '--json'))
    Invoke-Manager $cli ($globalArgs + @('skill', 'install', '--target', $Target, '--json'))
    Add-UserPath $binDir
    $env:PATH = $binDir + ';' + $env:PATH
    Push-Location -LiteralPath $env:USERPROFILE
    try {
        & todogc --help | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'TodoGC command-name check failed outside the project.' }
    } finally { Pop-Location }
    Invoke-Manager $cli ($globalArgs + @('doctor', '--runtime', '--json'))
    Write-Host "Installed TodoGC using ${manager}: $cli"
    Write-Host 'Command-name check passed outside the project without an activated environment.'
    Write-Host 'Open a new terminal (or restart your terminal app) to load the User PATH, then run: todogc doctor --runtime'
    Write-Host 'An independently opened terminal has not been verified by this installer.'
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
}
