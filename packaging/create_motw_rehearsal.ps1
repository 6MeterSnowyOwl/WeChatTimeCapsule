[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ZipPath,
    [Parameter(Mandatory=$true)][string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$SourceZip = (Resolve-Path -LiteralPath $ZipPath).Path
$Output = [IO.Path]::GetFullPath($OutputDirectory)
if (-not (Test-Path -LiteralPath $Output)) { New-Item -ItemType Directory -Path $Output | Out-Null }
$Copy = Join-Path $Output "motw-rehearsal-copy.zip"
$Extracted = Join-Path $Output "extracted"
Copy-Item -LiteralPath $SourceZip -Destination $Copy -Force
if (Test-Path -LiteralPath $Extracted) { Remove-Item -LiteralPath $Extracted -Recurse -Force }
New-Item -ItemType Directory -Path $Extracted | Out-Null
Set-Content -LiteralPath $Copy -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3`r`n" -Encoding ascii

$Shell = New-Object -ComObject Shell.Application
$ZipNamespace = $Shell.NameSpace($Copy)
$DestinationNamespace = $Shell.NameSpace($Extracted)
if ($null -eq $ZipNamespace -or $null -eq $DestinationNamespace) { throw "Explorer ZIP namespace unavailable" }
$DestinationNamespace.CopyHere($ZipNamespace.Items(), 20)
$ExpectedExe = Join-Path $Extracted "$([IO.Path]::GetFileNameWithoutExtension($SourceZip))\微信时间胶囊.exe"
for ($Index = 0; $Index -lt 200 -and -not (Test-Path -LiteralPath $ExpectedExe); $Index++) {
    Start-Sleep -Milliseconds 100
}
if (-not (Test-Path -LiteralPath $ExpectedExe)) { throw "Explorer-compatible extraction did not produce the EXE" }
$ZipZone = Get-Content -LiteralPath $Copy -Stream Zone.Identifier -Raw -ErrorAction SilentlyContinue
$ExeZone = Get-Content -LiteralPath $ExpectedExe -Stream Zone.Identifier -Raw -ErrorAction SilentlyContinue
$Payload = [ordered]@{
    schema_version = 1
    final_zip_unchanged = $true
    rehearsal_copy_has_internet_zone = $ZipZone -match 'ZoneId=3'
    extracted_exe_has_internet_zone = $ExeZone -match 'ZoneId=3'
    smartscreen_observation = "MANUAL_OBSERVATION_REQUIRED"
    post_upload_validation = "PENDING_POST_UPLOAD"
}
$Payload | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Output "motw-rehearsal.json") -Encoding utf8
Write-Output "COPY_MOTW=$($Payload.rehearsal_copy_has_internet_zone)"
Write-Output "EXE_MOTW=$($Payload.extracted_exe_has_internet_zone)"
Write-Output "SMARTSCREEN=MANUAL_OBSERVATION_REQUIRED"
