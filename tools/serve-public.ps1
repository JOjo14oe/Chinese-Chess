<#
.SYNOPSIS
    中国象棋联网对战：一键把本机服务暴露到公网（Cloudflare Tunnel）。

.DESCRIPTION
    只做部署，不改动任何业务代码。流程：
      1. 准备 cloudflared（本地 tools\bin 有就用；没有则从官方 GitHub Release 下载并校验
         Authenticode 签名必须是 Cloudflare, Inc.）；
      2. 确保本机游戏服务器在监听（没有就自动 `python main.py --serve` 起来）；
      3. 建立 Cloudflare 快速隧道，打印给国外朋友用的公网 https 地址；
      4. Ctrl+C 结束时自动关闭隧道（若服务器是本脚本启动的也会一并关闭）。

.PARAMETER Port
    本机服务端口，默认 8000。

.PARAMETER Lan
    不使用隧道，改为让局域网/同网段直连：监听 0.0.0.0 并（在有管理员权限时）放行防火墙。
    适合朋友在同一局域网；跨国请用默认的隧道模式。

.PARAMETER NoServer
    只开隧道，不检查/启动本机游戏服务器（你已经自己开好了）。

.PARAMETER CloudflaredUrl
    可选：手动指定 cloudflared.exe 的下载直链（自动下载失败时使用）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\serve-public.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\serve-public.ps1 -Port 9000
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$Lan,
    [switch]$NoServer,
    [string]$CloudflaredUrl = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$ToolsDir = $PSScriptRoot
$ProjectDir = Split-Path -Parent $ToolsDir
$BinDir = Join-Path $ToolsDir 'bin'
$Cloudflared = Join-Path $BinDir 'cloudflared.exe'
$UrlFile = Join-Path $ToolsDir 'public-url.txt'
$LogFile = Join-Path $env:TEMP 'xiangqi-tunnel.log'

function Info($msg) { Write-Host $msg }
function Ok($msg) { Write-Host $msg -ForegroundColor Green }
function Warn($msg) { Write-Host $msg -ForegroundColor Yellow }
function Die($msg) { Write-Host $msg -ForegroundColor Red; exit 1 }

function Test-Server {
    param([int]$P)
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$P/api/health" -UseBasicParsing -TimeoutSec 3
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Get-Cloudflared {
    if (Test-Path -LiteralPath $Cloudflared) {
        $sig = Get-AuthenticodeSignature -FilePath $Cloudflared
        if ($sig.Status -eq 'Valid' -and $sig.SignerCertificate.Subject -like '*Cloudflare*') {
            return
        }
        Warn "已存在的 cloudflared.exe 签名校验未通过，将重新下载。"
    }
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

    # 下载顺序：用户指定 → 官方直链（大多数网络可用）→ GitHub API 兜底
    # （本机 github.com 直连会被重置，但未认证的 API 有速率限制，所以只作第二选择）
    $direct = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
    $sources = @()
    if ($CloudflaredUrl) { $sources += $CloudflaredUrl }
    $sources += $direct

    $downloaded = $false
    $release = $null
    foreach ($source in $sources) {
        try {
            Info "正在下载 cloudflared（约 53 MB）：$source"
            Invoke-WebRequest -Uri $source -OutFile $Cloudflared -TimeoutSec 900 -UseBasicParsing
            $downloaded = $true
            break
        } catch {
            Warn "下载失败：$($_.Exception.Message)"
        }
    }

    if (-not $downloaded) {
        Warn '直连 github.com 失败，改用 GitHub API 取直链（可能被限速）…'
        try {
            $release = (Invoke-WebRequest `
                    -Uri 'https://api.github.com/repos/cloudflare/cloudflared/releases/latest' `
                    -Headers @{ 'User-Agent' = 'xiangqi-deploy' } -UseBasicParsing -TimeoutSec 60).Content |
                ConvertFrom-Json
            $asset = $release.assets |
                Where-Object { $_.name -eq 'cloudflared-windows-amd64.exe' } | Select-Object -First 1
            if (-not $asset) { throw '该 release 里没有 windows-amd64 资产' }
            Invoke-WebRequest -Uri $asset.url `
                -Headers @{ 'User-Agent' = 'xiangqi-deploy'; 'Accept' = 'application/octet-stream' } `
                -OutFile $Cloudflared -MaximumRedirection 10 -TimeoutSec 900 -UseBasicParsing
            $downloaded = $true
        } catch {
            Warn "API 方式也失败：$($_.Exception.Message)"
        }
    }

    if (-not $downloaded) {
        Die ("无法自动下载 cloudflared。请手动下载 cloudflared-windows-amd64.exe 放到：`n  $Cloudflared`n" +
             "或用参数指定直链：-CloudflaredUrl <下载地址>`n" +
             "官方下载页：https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")
    }

    $sig = Get-AuthenticodeSignature -FilePath $Cloudflared
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notlike '*Cloudflare*') {
        Die "cloudflared 签名校验失败（$($sig.Status)），已中止。"
    }
    if ($release) {
        Ok ("已获取 cloudflared {0}（签名：{1}）" -f $release.tag_name, 'Cloudflare, Inc.')
    } else {
        Ok '已获取 cloudflared（签名：Cloudflare, Inc.）'
    }
}

# ---------------------------------------------------------------- 参数与环境
if ($Lan) { $BindHost = '0.0.0.0' } else { $BindHost = '127.0.0.1' }
$serverProc = $null
$tunnelProc = $null

try {
    if (-not $NoServer -and -not (Test-Server -P $Port)) {
        $python = (Get-Command python -ErrorAction SilentlyContinue)
        if (-not $python) { Die "找不到 python，无法启动游戏服务器。" }
        Info "本机 $Port 端口没有服务，正在启动：python main.py --serve --host $BindHost --port $Port"
        $serverProc = Start-Process -FilePath $python.Source `
            -ArgumentList @('main.py', '--serve', '--host', $BindHost, '--port', "$Port") `
            -WorkingDirectory $ProjectDir -PassThru -WindowStyle Minimized
        $deadline = (Get-Date).AddSeconds(20)
        while (-not (Test-Server -P $Port)) {
            if ((Get-Date) -gt $deadline) { Die "游戏服务器启动超时，请手动运行 python main.py --serve 看看报错。" }
            Start-Sleep -Milliseconds 400
        }
        Ok "游戏服务器已就绪：http://127.0.0.1:$Port/"
    } elseif (Test-Server -P $Port) {
        Ok "检测到本机 $Port 端口已有服务，直接复用。"
    }

    if ($Lan) {
        $lanIp = (Get-NetIPAddress -AddressFamily IPv4 |
            Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
            Select-Object -First 1 -ExpandProperty IPAddress)
        $isAdmin = ([Security.Principal.WindowsPrincipal] `
                [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
        if ($isAdmin) {
            $rule = Get-NetFirewallRule -DisplayName 'Xiangqi 8000' -ErrorAction SilentlyContinue
            if (-not $rule) {
                New-NetFirewallRule -DisplayName 'Xiangqi 8000' -Direction Inbound -Protocol TCP `
                    -LocalPort $Port -Action Allow | Out-Null
                Ok "已添加入站防火墙规则 'Xiangqi 8000'（TCP $Port）"
            }
        } else {
            Warn "非管理员：若要局域网访问，请以管理员运行一次："
            Warn "  New-NetFirewallRule -DisplayName 'Xiangqi 8000' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow"
        }
        Write-Host ''
        Write-Host '==================== 局域网直连模式 ====================' -ForegroundColor Cyan
        Ok ("把下面地址发给同一网络里的朋友： http://{0}:{1}/" -f $lanIp, $Port)
        Info '自己也可以继续用： http://127.0.0.1:' + $Port + '/'
        Info '跨国请在同一个脚本里去掉 -Lan（走 Cloudflare 隧道）。'
        Write-Host '=======================================================' -ForegroundColor Cyan
        if (-not $NoServer) { Info '按 Ctrl+C 结束（本脚本启动的服务器会一并关闭）。' }
        while ($true) { Start-Sleep -Seconds 3600 }
    }

    Get-Cloudflared

    Info "正在建立 Cloudflare 隧道 → http://127.0.0.1:$Port …"
    if (Test-Path -LiteralPath $LogFile) { Remove-Item -LiteralPath $LogFile -Force }
    $tunnelProc = Start-Process -FilePath $Cloudflared `
        -ArgumentList @('tunnel', '--url', "http://127.0.0.1:$Port", '--no-autoupdate', '--loglevel', 'info') `
        -PassThru -WindowStyle Hidden -RedirectStandardError $LogFile -RedirectStandardOutput "$LogFile.out"

    $url = $null
    $deadline = (Get-Date).AddSeconds(60)
    while (-not $url) {
        if ((Get-Date) -gt $deadline) {
            Warn "60 秒内没拿到公网地址，隧道日志：$LogFile"
            if (Test-Path -LiteralPath $LogFile) { Get-Content -LiteralPath $LogFile -Tail 25 | ForEach-Object { Warn "  $_" } }
            Die "隧道建立失败。常见原因：本机到 Cloudflare 的 UDP 7844/TCP 443 被封；可加 --protocol http2 重试。"
        }
        Start-Sleep -Milliseconds 500
        if (Test-Path -LiteralPath $LogFile) {
            $text = Get-Content -LiteralPath $LogFile -Raw -ErrorAction SilentlyContinue
            if ($text -match 'https://([a-z0-9-]+)\.trycloudflare\.com') {
                $url = $Matches[0]
            }
        }
    }
    Set-Content -LiteralPath $UrlFile -Value $url -Encoding ASCII

    Write-Host ''
    Write-Host '================ 公网对战已就绪（Cloudflare 隧道） ================' -ForegroundColor Cyan
    Ok ("发给国外朋友： {0}/" -f $url)
    Info '朋友打开后：点「加入房间」并输入你的 6 位房间号（或直接点你发的 ?room=XXXXXX 链接）。'
    Info ("你自己也打开： {0}/  → 点「创建房间」→ 把房间号/链接发给对方" -f $url)
    Info ''
    Info '传输说明（经 Cloudflare 快速隧道的实测结论）：'
    Info '  · WebSocket 正常 —— 浏览器会自动优先用它，对战体验最好；'
    Info '  · 长轮询正常 —— 备用通道，实测被对手走子唤醒约 2～3 秒；'
    Info '  · SSE 会被快速隧道整包缓冲（不实时），客户端约 60 秒后自动降级到长轮询，不影响能下棋。'
    Info ''
    Info "公网地址也保存在： $UrlFile"
    Info '注意：快速隧道是临时地址（重启隧道就变），无 SLA；对局数据全在你本机内存里。'
    Warn '按 Ctrl+C 结束隧道。'
    Write-Host '=================================================================' -ForegroundColor Cyan

    Wait-Process -Id $tunnelProc.Id
} finally {
    Write-Host ''
    Info '正在收尾…'
    if ($tunnelProc -and -not $tunnelProc.HasExited) {
        Stop-Process -Id $tunnelProc.Id -Force -ErrorAction SilentlyContinue
        Info '隧道已关闭。'
    }
    if ($serverProc -and -not $serverProc.HasExited) {
        Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue
        Info '游戏服务器已关闭。'
    }
}
