@echo off
rem ============================================================================
rem  Xiangqi (Chinese Chess) - one-click public match via Cloudflare Tunnel.
rem
rem  This file is intentionally ASCII-ONLY: cmd.exe reads .cmd/.bat files using
rem  the active OEM code page (936/GBK on a Chinese Windows), so any UTF-8
rem  Chinese text here would be decoded into garbage and executed as commands.
rem  All Chinese messages come from serve-public.ps1 (saved as UTF-8 with BOM).
rem ============================================================================
setlocal
chcp 65001 >nul 2>nul
pushd "%~dp0"

echo.
echo   Xiangqi / Chinese Chess - one-click public match (Cloudflare Tunnel)
echo   ------------------------------------------------------------------
echo   First run downloads cloudflared (~53 MB) and verifies its signature.
echo   The public https URL will be printed when it is ready.
echo   Press Ctrl+C to stop the tunnel.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0serve-public.ps1" %*

if errorlevel 1 (
  echo.
  echo   [notice] The script ended. If you pressed Ctrl+C, the tunnel is now
  echo            closed and that is expected.
  echo   If it failed because of the PowerShell execution policy, run:
  echo     powershell -NoProfile -ExecutionPolicy Bypass -File tools\serve-public.ps1
  echo   Non-ASCII text above looking garbled? Run "chcp 65001" first.
)

popd
pause
