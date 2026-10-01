@echo off
title VaultBox Server Tracking Web
cd /d "%~dp0"

echo ================================================================
echo             VAULTBOX SERVER TRACKING (WEB DASHBOARD)
echo ================================================================
echo.
echo [*] Starting local web server...

:: Check if Python is installed
python --version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [+] Python detected. Launching HTTP server on port 5173...
    start "" http://localhost:5173
    python -m http.server 5173
    goto END
)

:: Check if Node is installed
node --version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [+] Node.js detected. Launching HTTP server on port 5173...
    start "" http://localhost:5173
    npx -y serve -p 5173
    goto END
)

:: Fallback if neither is available: open index.html directly
echo [!] Neither Python nor Node.js found in PATH. Opening directly in browser...
start "" index.html

:END
pause
