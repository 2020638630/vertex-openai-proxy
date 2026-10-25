@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0."

set PORT=%1
if "%PORT%"=="" set PORT=4000

set PID=
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr LISTENING') do set PID=%%p

if "%PID%"=="" (
    echo [提示] %PORT% 端口没有监听进程，服务可能没在运行
    if not "%NO_PAUSE%"=="1" pause
    popd
    exit /b 0
)

echo 正在停止 PID %PID%（端口 %PORT%）...
taskkill /PID %PID% /F >nul 2>nul
if errorlevel 1 (
    echo [错误] 停止失败，可能需要管理员权限
    if not "%NO_PAUSE%"=="1" pause
    popd
    exit /b 1
)

echo [OK] 已停止
if not "%NO_PAUSE%"=="1" pause
popd
