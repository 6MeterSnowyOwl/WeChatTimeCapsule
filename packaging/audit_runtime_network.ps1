[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ExePath,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [int]$ObserveSeconds = 12
)

$ErrorActionPreference = "Stop"
$Exe = (Resolve-Path -LiteralPath $ExePath).Path
$Process = Start-Process -FilePath $Exe -PassThru
$AppTcp = @()
$AppUdp = @()
$WeixinTcp = @()
$WeixinUdp = @()
try {
    for ($Index = 0; $Index -lt $ObserveSeconds; $Index++) {
        Start-Sleep -Seconds 1
        $Process.Refresh()
        if ($Process.HasExited) { throw "Packaged executable exited during network observation" }
        $WeixinPids = @(Get-Process -Name Weixin -ErrorAction SilentlyContinue | ForEach-Object Id)
        $AppTcp += @(Get-NetTCPConnection -OwningProcess $Process.Id -ErrorAction SilentlyContinue |
            Where-Object { $_.RemoteAddress -notin @("0.0.0.0", "::") } |
            Select-Object State,RemoteAddress,RemotePort)
        $AppUdp += @(Get-NetUDPEndpoint -OwningProcess $Process.Id -ErrorAction SilentlyContinue |
            Select-Object LocalPort)
        foreach ($WeixinPid in $WeixinPids) {
            $WeixinTcp += @(Get-NetTCPConnection -OwningProcess $WeixinPid -ErrorAction SilentlyContinue |
                Where-Object { $_.RemoteAddress -notin @("0.0.0.0", "::") } |
                Select-Object State,RemoteAddress,RemotePort)
            $WeixinUdp += @(Get-NetUDPEndpoint -OwningProcess $WeixinPid -ErrorAction SilentlyContinue |
                Select-Object LocalPort)
        }
    }
    $Payload = [ordered]@{
        schema_version = 1
        observation_seconds = $ObserveSeconds
        app = [ordered]@{
            process = "微信时间胶囊.exe"
            tcp_outbound_connection_count = @($AppTcp | Sort-Object RemoteAddress,RemotePort -Unique).Count
            udp_endpoint_count = @($AppUdp | Sort-Object LocalPort -Unique).Count
        }
        weixin = [ordered]@{
            process = "Weixin.exe"
            tcp_outbound_connection_count = @($WeixinTcp | Sort-Object RemoteAddress,RemotePort -Unique).Count
            udp_endpoint_count = @($WeixinUdp | Sort-Object LocalPort -Unique).Count
        }
        attribution = "PID-specific; Weixin traffic is not attributed to the application"
    }
    $Payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $OutputPath -Encoding utf8
    Write-Output "APP_TCP_OUTBOUND=$($Payload.app.tcp_outbound_connection_count)"
    Write-Output "APP_UDP_ENDPOINTS=$($Payload.app.udp_endpoint_count)"
} finally {
    if (-not $Process.HasExited) {
        $null = $Process.CloseMainWindow()
        if (-not $Process.WaitForExit(5000)) { Stop-Process -Id $Process.Id -Force }
    }
}
