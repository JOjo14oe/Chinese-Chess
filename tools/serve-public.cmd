@echo off
rem 中国象棋联网对战：双击即可把本机服务暴露到公网（Cloudflare Tunnel）
rem 本机 PowerShell 执行策略是 Restricted，所以这里显式用 -ExecutionPolicy Bypass 调用。
setlocal
echo.
echo   中国象棋 · 一键公网对战（Cloudflare Tunnel）
echo   ------------------------------------------------
echo   首次运行会自动下载 cloudflared（约 53 MB）并校验签名。
echo   结束请按 Ctrl+C。
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0serve-public.ps1" %*
if errorlevel 1 (
  echo.
  echo   [脚本以非零状态结束] 若提示执行策略问题，请改用：
  echo     powershell -NoProfile -ExecutionPolicy Bypass -File tools\serve-public.ps1
)
pause
