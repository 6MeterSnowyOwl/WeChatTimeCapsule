[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ReleaseDirectory,
    [string]$ZipPath,
    [string]$SourceZipPath,
    [string]$ThirdPartySourceDirectory,
    [switch]$LaunchSmoke
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ReleaseDirectory = (Resolve-Path -LiteralPath $ReleaseDirectory).Path
$VenvPython = Join-Path $ProjectRoot ".venv-release-py312\Scripts\python.exe"
$ConfigJson = & $VenvPython (Join-Path $PSScriptRoot "release_config.py") $ProjectRoot (Join-Path $PSScriptRoot "windows_version_info.txt")
if ($LASTEXITCODE -ne 0) { throw "Release version configuration validation failed" }
$Config = $ConfigJson | ConvertFrom-Json
if ((Split-Path $ReleaseDirectory -Leaf) -ne $Config.release_name) { throw "Release directory/configuration mismatch" }
$Expected = @(
    "微信时间胶囊.exe", "_internal", "README.txt", "LICENSE", "THIRD_PARTY_LICENSES.txt",
    "THIRD_PARTY_LICENSES", "THIRD_PARTY_SOURCE.md", "RELEASE_NOTES.txt", "BUILD_INFO.json"
)
foreach ($Name in $Expected) {
    if (-not (Test-Path -LiteralPath (Join-Path $ReleaseDirectory $Name))) {
        throw "Missing expected release item: $Name"
    }
}

$ForbiddenDirectories = @("exports", "state", "snapshots", "diagnostics", "logs", "outputs", "work", "tests", ".venv", ".venv-release-py312", "__pycache__", ".pytest_cache")
$ForbiddenExtensions = @(".db", ".sqlite", ".sqlite3", ".jsonl", ".pdb", ".spec")
$ForbiddenHits = @(Get-ChildItem -LiteralPath $ReleaseDirectory -Recurse -Force | Where-Object {
    ($_.PSIsContainer -and $ForbiddenDirectories -contains $_.Name.ToLowerInvariant()) -or
    (-not $_.PSIsContainer -and $ForbiddenExtensions -contains $_.Extension.ToLowerInvariant())
})
if ($ForbiddenHits.Count -ne 0) { throw "Forbidden development artifacts found: $($ForbiddenHits.Count)" }
$ForbiddenQtHits = @(Get-ChildItem -LiteralPath $ReleaseDirectory -Recurse -File | Where-Object {
    $_.Name.ToLowerInvariant() -in @("qt6virtualkeyboard.dll", "qtvirtualkeyboardplugin.dll")
})
if ($ForbiddenQtHits.Count -ne 0) { throw "Unused GPL-only Qt VirtualKeyboard binary shipped" }

$PrivatePatterns = @('wx' + 'id_')
if (-not [string]::IsNullOrWhiteSpace($env:WTC_PRIVATE_PATTERNS_JSON)) {
    $PrivatePatterns += @($env:WTC_PRIVATE_PATTERNS_JSON | ConvertFrom-Json)
}
function Test-Privacy([string]$Root) {
    $Hits = 0
    Get-ChildItem -LiteralPath $Root -Recurse -File | ForEach-Object {
        $NameText = $_.FullName.Substring($Root.Length)
        foreach ($Pattern in $PrivatePatterns) {
            if ($NameText.IndexOf($Pattern, [StringComparison]::Ordinal) -ge 0) { $Hits++ }
        }
        $Bytes = [IO.File]::ReadAllBytes($_.FullName)
        $Utf8 = [Text.Encoding]::UTF8.GetString($Bytes)
        $Utf16 = [Text.Encoding]::Unicode.GetString($Bytes)
        foreach ($Pattern in $PrivatePatterns) {
            if ($Pattern -eq $env:WTC_PRIVATE_CONTACT_NAME -and $_.Name -match '^icudt\d+\.dll$') { continue }
            if ($Utf8.IndexOf($Pattern, [StringComparison]::Ordinal) -ge 0 -or
                $Utf16.IndexOf($Pattern, [StringComparison]::Ordinal) -ge 0) { $Hits++ }
        }
    }
    return $Hits
}
$PrivacyHits = Test-Privacy $ReleaseDirectory
if ($PrivacyHits -ne 0) { throw "Known private identity scan failed with $PrivacyHits hit(s)" }

$RootLicenseHash = (Get-FileHash -LiteralPath (Join-Path $ProjectRoot "LICENSE") -Algorithm SHA256).Hash
$ReleaseLicenseHash = (Get-FileHash -LiteralPath (Join-Path $ReleaseDirectory "LICENSE") -Algorithm SHA256).Hash
if ($RootLicenseHash -ne $ReleaseLicenseHash) { throw "Apache-2.0 LICENSE mismatch" }

$ProjectLicenses = @(Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "THIRD_PARTY_LICENSES") -Recurse -File)
$ReleaseLicenses = @(Get-ChildItem -LiteralPath (Join-Path $ReleaseDirectory "THIRD_PARTY_LICENSES") -Recurse -File)
if ($ProjectLicenses.Count -ne 66 -or $ReleaseLicenses.Count -ne 66) {
    throw "Third-party license tree must contain 66 files in source and release"
}
$ProjectRelative = @($ProjectLicenses | ForEach-Object { $_.FullName.Substring((Join-Path $ProjectRoot "THIRD_PARTY_LICENSES").Length + 1) } | Sort-Object)
$ReleaseRelative = @($ReleaseLicenses | ForEach-Object { $_.FullName.Substring((Join-Path $ReleaseDirectory "THIRD_PARTY_LICENSES").Length + 1) } | Sort-Object)
if (Compare-Object $ProjectRelative $ReleaseRelative) { throw "Third-party license tree names mismatch" }

$Info = Get-Content -LiteralPath (Join-Path $ReleaseDirectory "BUILD_INFO.json") -Raw | ConvertFrom-Json
if ($Info.version -ne $Config.version -or $Info.display_version -ne "v$($Config.display_version)" -or $Info.platform -ne "windows-x64" -or $Info.python -ne "3.12.14") { throw "BUILD_INFO validation failed" }
if (@($Info.historically_tested_wechat_versions) -notcontains "4.1.13.63" -or @($Info.historically_tested_wechat_versions) -notcontains "4.1.13.65" -or $Info.rc7_wechat_retest -ne "NOT_PERFORMED") { throw "Historical WeChat version metadata is incomplete" }
if ($Info.license_distribution_check -ne "BLOCKED" -or $Info.ffmpeg_corresponding_source -ne "BLOCKED" -or $Info.public_release_ready -ne $false) { throw "Unresolved RC7 distribution gates must remain BLOCKED" }
if ($Info.upx -ne $false -or $Info.signed -ne $false -or $Info.format -ne "PyInstaller onedir") { throw "Packaging mode validation failed" }
if (($Info | ConvertTo-Json -Depth 5) -match 'Users\\') { throw "Private build path found in BUILD_INFO" }

$Exe = Join-Path $ReleaseDirectory "微信时间胶囊.exe"
$Version = (Get-Item -LiteralPath $Exe).VersionInfo
if ($Version.FileVersion -ne $Config.file_version -or $Version.ProductVersion -ne $Config.version -or $Version.ProductName -ne "微信时间胶囊") { throw "Executable version resource validation failed" }

$ExeSidecar = Join-Path (Split-Path $ReleaseDirectory -Parent) "微信时间胶囊.exe.sha256"
if (-not (Test-Path -LiteralPath $ExeSidecar)) { throw "Executable SHA-256 sidecar missing" }
$ExpectedExeHash = ((Get-Content -LiteralPath $ExeSidecar -Raw).Trim() -split '\s+')[0]
$ActualExeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Exe).Hash
if ($ExpectedExeHash -ne $ActualExeHash) { throw "Executable SHA-256 sidecar mismatch" }

Add-Type -AssemblyName System.IO.Compression.FileSystem
if ($ZipPath) {
    $ZipPath = (Resolve-Path -LiteralPath $ZipPath).Path
    $ZipSidecar = "$ZipPath.sha256"
    if (-not (Test-Path -LiteralPath $ZipSidecar)) { throw "ZIP SHA-256 sidecar missing" }
    $ExpectedZipHash = ((Get-Content -LiteralPath $ZipSidecar -Raw).Trim() -split '\s+')[0]
    $ActualZipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash
    if ($ExpectedZipHash -ne $ActualZipHash) { throw "ZIP SHA-256 sidecar mismatch" }
    $Zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $Names = @($Zip.Entries | ForEach-Object FullName)
        $Prefix = [IO.Path]::GetFileNameWithoutExtension($ZipPath)
        foreach ($Name in @("$Prefix/微信时间胶囊.exe", "$Prefix/README.txt", "$Prefix/LICENSE")) {
            if ($Names -notcontains $Name) { throw "ZIP entry missing or Unicode damaged: $Name" }
        }
    } finally { $Zip.Dispose() }
}

if ($SourceZipPath) {
    $SourceZipPath = (Resolve-Path -LiteralPath $SourceZipPath).Path
    $Temp = Join-Path ([IO.Path]::GetTempPath()) ("wtc-source-audit-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $Temp | Out-Null
    try {
        [IO.Compression.ZipFile]::ExtractToDirectory($SourceZipPath, $Temp)
        $SourcePrivacyHits = Test-Privacy $Temp
        if ($SourcePrivacyHits -ne 0) { throw "Public source privacy scan failed with $SourcePrivacyHits hit(s)" }
        foreach ($Required in @("README.md", "LICENSE", "THIRD_PARTY_SOURCE.md", "wechat_exporter/__init__.py", "tests/test_release_gates.py")) {
            if (-not (Test-Path -LiteralPath (Join-Path $Temp $Required))) { throw "Source ZIP missing: $Required" }
        }
    } finally { Remove-Item -LiteralPath $Temp -Recurse -Force }
}

if ($ThirdPartySourceDirectory) {
    $ThirdPartySourceDirectory = (Resolve-Path -LiteralPath $ThirdPartySourceDirectory).Path
    $RequiredSources = [ordered]@{
        "qt-everywhere-src-6.11.2.tar.xz" = "6dcfbca271d76a6502741a2c0dc6fc98ef7dd0b7b4cfd0abcebb285a86a26f33"
        "pyside-setup-everywhere-src-6.11.2.zip" = "c0fdd62b91a1d36d5ee2e1fb71050a32fbc93fcdeef0fdcb41d29afaaf00d9b5"
    }
    foreach ($Entry in $RequiredSources.GetEnumerator()) {
        $Path = Join-Path $ThirdPartySourceDirectory $Entry.Key
        if (-not (Test-Path -LiteralPath $Path)) { throw "Corresponding source missing: $($Entry.Key)" }
        if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Entry.Value) { throw "Corresponding source hash mismatch: $($Entry.Key)" }
    }
    foreach ($Index in @("SOURCE_INDEX.txt", "SHA256SUMS.txt")) {
        if (-not (Test-Path -LiteralPath (Join-Path $ThirdPartySourceDirectory $Index))) { throw "Source index missing: $Index" }
    }
}

if ($LaunchSmoke) {
    $Started = Get-Date
    $Process = Start-Process -FilePath $Exe -PassThru
    try {
        $Ready = $false
        $ExpectedTitle = "微信时间胶囊 v$($Config.display_version)"
        for ($Index = 0; $Index -lt 200; $Index++) {
            Start-Sleep -Milliseconds 100
            $Process.Refresh()
            if ($Process.HasExited) { throw "Packaged executable exited during smoke test" }
            if ($Process.MainWindowHandle -ne 0 -and $Process.MainWindowTitle -eq $ExpectedTitle) { $Ready = $true; break }
        }
        if (-not $Ready) { throw "Packaged GUI did not show the expected main window within 20 seconds" }
        Write-Output "MAIN_WINDOW_TITLE=$($Process.MainWindowTitle)"
        Write-Output ("COLD_START_SECONDS={0:N2}" -f ((Get-Date) - $Started).TotalSeconds)
        $null = $Process.CloseMainWindow()
        if (-not $Process.WaitForExit(5000)) { Stop-Process -Id $Process.Id -Force }
    } finally {
        if (-not $Process.HasExited) { Stop-Process -Id $Process.Id -Force }
    }
}

Write-Output "APACHE_LICENSE=PASS"
Write-Output "THIRD_PARTY_LICENSE_FILES=66"
Write-Output "FORBIDDEN_ARTIFACT_HITS=0"
Write-Output "BINARY_PRIVACY_HITS=0"
if ($SourceZipPath) { Write-Output "SOURCE_PRIVACY_HITS=0" }
if ($ThirdPartySourceDirectory) { Write-Output "QT_PYSIDE_CORRESPONDING_SOURCE=PASS" }
Write-Output "EXCLUDED_QT_VIRTUALKEYBOARD=PASS"
Write-Output "FFMPEG_CORRESPONDING_SOURCE=BLOCKED"
Write-Output "LICENSE_DISTRIBUTION_CHECK=BLOCKED"
Write-Output "PUBLIC_RELEASE_READY=NO"
Write-Output "BUILD_INFO=PASS"
Write-Output "VERSION_RESOURCE=PASS"
Write-Output "HASH_SIDECARS=PASS"
Write-Output "VERIFY_RELEASE=PASS"
