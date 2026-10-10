[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ZipPath,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [Parameter(Mandatory=$true)][ValidateSet(0,1)][int]$AppStarted,
    [Parameter(Mandatory=$true)][int]$ExitCode,
    [Parameter(Mandatory=$true)][ValidateSet(0,1)][int]$DllError
)

$ErrorActionPreference = "Stop"
$Zip = Resolve-Path -LiteralPath $ZipPath
$Version = "0.3.1-rc.7"
$PythonInstalled = $null -ne (Get-Command python.exe -ErrorAction SilentlyContinue) -or
                   $null -ne (Get-Command py.exe -ErrorAction SilentlyContinue)
$Payload = [ordered]@{
    windows_version = [Environment]::OSVersion.VersionString
    architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    python_installed = [bool]$PythonInstalled
    app_started = [bool]$AppStarted
    exit_code = $ExitCode
    dll_error = [bool]$DllError
    package_version = $Version
    package_sha256 = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
}
$Payload | ConvertTo-Json | Set-Content -LiteralPath $OutputPath -Encoding utf8
Write-Output "CLEAN_MACHINE_RESULT=$OutputPath"
