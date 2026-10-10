[CmdletBinding()]
param(
    [string]$Python312 = "",
    [switch]$SkipInstall,
    [switch]$FreshVenv
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvRoot = Join-Path $ProjectRoot ".venv-release-py312"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"
$WorkRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot "work\release-v0.3.1-rc7"))
$StageRoot = Join-Path $WorkRoot "source"
$BuildRoot = Join-Path $WorkRoot "pyinstaller-build"
$DistRoot = Join-Path $WorkRoot "pyinstaller-dist"
$OutputRoot = [IO.Path]::GetFullPath((Join-Path (Split-Path $ProjectRoot -Parent) "outputs\release\v0.3.1-rc7"))
$ReleaseName = "微信时间胶囊-v0.3.1-rc7-win64"
$ReleaseDirectory = Join-Path $OutputRoot $ReleaseName
$ZipPath = Join-Path $OutputRoot "$ReleaseName.zip"
$ThirdPartySource = (Join-Path (Split-Path $ProjectRoot -Parent) "outputs\release-compliance\third-party-source")

if (Test-Path -LiteralPath $OutputRoot) { throw "RC7 release output already exists; preserve it and choose a separately authorized build destination" }
if ($FreshVenv) { throw "RC7 reuses the fixed release environment; -FreshVenv is not permitted" }
if (-not (Test-Path -LiteralPath $VenvPython)) {
    if ([string]::IsNullOrWhiteSpace($Python312)) { throw "Pass -Python312 with a CPython 3.12 x64 executable" }
    if (-not (Test-Path -LiteralPath $Python312)) { throw "Python 3.12 x64 not found: $Python312" }
    & $Python312 -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) { throw "Release environment creation failed" }
}
$PythonCheck = & $VenvPython -c "import platform,struct,sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}|{platform.machine()}|{struct.calcsize(chr(80))*8}')"
if ($LASTEXITCODE -ne 0 -or $PythonCheck -notmatch '^3\.12\.14\|AMD64\|64$') { throw "RC7 release Python must be CPython 3.12.14 x64; got $PythonCheck" }
$ConfigJson = & $VenvPython (Join-Path $PSScriptRoot "release_config.py") $ProjectRoot (Join-Path $PSScriptRoot "windows_version_info.txt")
if ($LASTEXITCODE -ne 0) { throw "Release version configuration validation failed" }
$Config = $ConfigJson | ConvertFrom-Json
if ($ReleaseName -ne $Config.release_name -or (Split-Path $OutputRoot -Leaf) -ne $Config.release_tag -or
    (Split-Path $WorkRoot -Leaf) -ne "release-$($Config.release_tag)") { throw "Release path/configuration mismatch" }

if (-not $SkipInstall) {
    $env:PIP_CACHE_DIR = Join-Path $WorkRoot "pip-cache"
    & $VenvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt") -r (Join-Path $ProjectRoot "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "Release dependency installation failed" }
}
$Toolchain = & $VenvPython -c "from importlib import metadata; print('|'.join(metadata.version(name) for name in ('PyInstaller','PySide6','shiboken6','wechatauto-replica')))"
if ($LASTEXITCODE -ne 0 -or $Toolchain -ne "6.22.2|6.11.2|6.11.2|1.2.1") { throw "RC7 fixed release dependency mismatch" }
& $VenvPython (Join-Path $PSScriptRoot "stage_corresponding_source.py") $ThirdPartySource
if ($LASTEXITCODE -ne 0) { throw "Qt/PySide corresponding source staging failed" }

foreach ($Path in @($StageRoot, $BuildRoot, $DistRoot)) {
    $Path = [IO.Path]::GetFullPath($Path)
    if (-not $Path.StartsWith($WorkRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Work cleanup target is outside dedicated RC7 work directory"
    }
    $Ancestor = $Path
    while ($Ancestor -and $Ancestor.Length -ge $ProjectRoot.Length) {
        if (Test-Path -LiteralPath $Ancestor) {
            if ((Get-Item -LiteralPath $Ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing RC7 work cleanup through a reparse point"
            }
        }
        if ($Ancestor -eq $ProjectRoot) { break }
        $Ancestor = Split-Path $Ancestor -Parent
    }
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
}
New-Item -ItemType Directory -Path $OutputRoot | Out-Null

# Strict source allowlist: one entry point plus Python application modules only.
Copy-Item -LiteralPath (Join-Path $ProjectRoot "gui.py") -Destination (Join-Path $StageRoot "gui.py")
$PackageSource = Join-Path $ProjectRoot "wechat_exporter"
Get-ChildItem -LiteralPath $PackageSource -Recurse -File -Filter "*.py" | ForEach-Object {
    $Relative = $_.FullName.Substring($ProjectRoot.Length + 1)
    $Destination = Join-Path $StageRoot $Relative
    New-Item -ItemType Directory -Path (Split-Path $Destination -Parent) -Force | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $Destination
}
$MetadataRoot = Join-Path $StageRoot "wechatauto_replica-1.2.1.dist-info"
New-Item -ItemType Directory -Path $MetadataRoot -Force | Out-Null
@("Metadata-Version: 2.1", "Name: wechatauto-replica", "Version: 1.2.1", "") |
    Set-Content -LiteralPath (Join-Path $MetadataRoot "METADATA") -Encoding utf8

$LicenseTarget = Join-Path $ProjectRoot "THIRD_PARTY_LICENSES"
& $VenvPython (Join-Path $PSScriptRoot "collect_licenses.py") $LicenseTarget --legal-dir (Join-Path $PSScriptRoot "legal")
if ($LASTEXITCODE -ne 0) { throw "Third-party notice collection failed" }

$env:WTC_STAGE_ROOT = $StageRoot
$env:WTC_WORK_ROOT = $BuildRoot
$env:WTC_VERSION_FILE = Join-Path $PSScriptRoot "windows_version_info.txt"
$env:WTC_MANIFEST_FILE = Join-Path $PSScriptRoot "windows_manifest.xml"
$env:SOURCE_DATE_EPOCH = "1767225600"
$env:PYTHONHASHSEED = "0"
# Prevent unrelated developer-tool DLLs on PATH from contaminating dependency
# discovery (for example, an incompatible ICU from a document toolchain).
$env:PATH = @(
    (Join-Path $VenvRoot "Scripts"),
    (Join-Path $env:WINDIR "System32"),
    $env:WINDIR,
    (Join-Path $env:WINDIR "System32\Wbem"),
    (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0")
) -join ";"
& $VenvPython -m PyInstaller --clean --noconfirm --workpath $BuildRoot --distpath $DistRoot (Join-Path $PSScriptRoot "wechat_time_capsule.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$BuiltDirectory = Join-Path $DistRoot $ReleaseName
if (-not (Test-Path -LiteralPath $BuiltDirectory)) { throw "PyInstaller output missing: $BuiltDirectory" }
Move-Item -LiteralPath $BuiltDirectory -Destination $ReleaseDirectory
Copy-Item -LiteralPath (Join-Path $ProjectRoot "LICENSE") -Destination (Join-Path $ReleaseDirectory "LICENSE")
Copy-Item -LiteralPath (Join-Path $ProjectRoot "THIRD_PARTY_LICENSES.txt") -Destination (Join-Path $ReleaseDirectory "THIRD_PARTY_LICENSES.txt")
Copy-Item -LiteralPath (Join-Path $ProjectRoot "THIRD_PARTY_SOURCE.md") -Destination (Join-Path $ReleaseDirectory "THIRD_PARTY_SOURCE.md")
Copy-Item -LiteralPath $LicenseTarget -Destination (Join-Path $ReleaseDirectory "THIRD_PARTY_LICENSES") -Recurse
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "README.txt") -Destination (Join-Path $ReleaseDirectory "README.txt")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "RELEASE_NOTES.txt") -Destination (Join-Path $ReleaseDirectory "RELEASE_NOTES.txt")

$PipPackages = & $VenvPython -m pip freeze --all
$PipPackages | Set-Content -LiteralPath (Join-Path $WorkRoot "release-packages.txt") -Encoding utf8
$PyInstallerVersion = (& $VenvPython -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
$BuildInfo = [ordered]@{
    product = "微信时间胶囊"
    version = $Config.version
    display_version = "v$($Config.display_version)"
    platform = "windows-x64"
    python = $PythonCheck.Split('|')[0]
    pyinstaller = $PyInstallerVersion
    pyside6 = "6.11.2"
    shiboken6 = "6.11.2"
    historically_tested_wechat_versions = @("4.1.13.63", "4.1.13.65")
    rc7_wechat_retest = "NOT_PERFORMED"
    windows_development_host = "Windows 11"
    clean_machine = "BLOCKED"
    upstream = [ordered]@{ "wechatauto-replica" = "1.2.1" }
    build_time = (Get-Date).ToUniversalTime().ToString("o")
    source_commit = $null
    source_tree = "not-a-git-repository"
    format = "PyInstaller onedir"
    upx = $false
    signed = $false
    requested_execution_level = "asInvoker"
    final_icon = "pending"
    license_gate = "BLOCKED"
    license_distribution_check = "BLOCKED"
    ffmpeg_corresponding_source = "BLOCKED"
    ffmpeg_runtime = "imageio-ffmpeg bundled Gyan FFmpeg 7.1; GPL-3.0-or-later; static x264/x265"
    excluded_unused_qt_binaries = @("Qt6VirtualKeyboard.dll", "qtvirtualkeyboardplugin.dll")
    public_release_ready = $false
}
$BuildInfo | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $ReleaseDirectory "BUILD_INFO.json") -Encoding utf8

$ExePath = Join-Path $ReleaseDirectory "微信时间胶囊.exe"
$ExeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ExePath).Hash.ToLowerInvariant()
Set-Content -LiteralPath (Join-Path $OutputRoot "微信时间胶囊.exe.sha256") -Value "$ExeHash  微信时间胶囊.exe" -Encoding ascii

Compress-Archive -LiteralPath $ReleaseDirectory -DestinationPath $ZipPath -CompressionLevel Optimal
$ZipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$ZipPath.sha256" -Value "$ZipHash  $ReleaseName.zip" -Encoding ascii

& (Join-Path $PSScriptRoot "verify_release.ps1") -ReleaseDirectory $ReleaseDirectory -ZipPath $ZipPath -ThirdPartySourceDirectory $ThirdPartySource
Write-Output "RELEASE_DIRECTORY=$ReleaseDirectory"
Write-Output "ZIP_PATH=$ZipPath"
