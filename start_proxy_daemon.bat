@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0."

set PORT=%PROXY_PORT%
if "%PORT%"=="" set PORT=4000

rem 端口已被占用就不再启动，避免第二个进程静默退出
netstat -ano | findstr ":%PORT%" | findstr LISTENING >nul
if not errorlevel 1 (
    echo [提示] %PORT% 端口已在监听，服务可能已在运行
    echo         需要重启请先运行 stop_proxy.bat %PORT%
    if not "%NO_PAUSE%"=="1" pause
    popd
    exit /b 1
)

if not exist "logs" mkdir logs

echo 正在后台启动代理（端口 %PORT%）...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:PROXY_PORT='%PORT%'; Start-Process -FilePath python -ArgumentList 'vertex_openai_proxy.py' -WorkingDirectory '%CD%' -WindowStyle Hidden -RedirectStandardOutput 'logs\console.out.log' -RedirectStandardError 'logs\console.err.log'"
if errorlevel 1 (
    echo [错误] 启动失败
    if not "%NO_PAUSE%"=="1" pause
    popd
    exit /b 1
)

echo 等待服务就绪...
powershell -NoProfile -Command "for ($i=0; $i -lt 25; $i++) { try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/healthz' -TimeoutSec 2 -UseBasicParsing; Write-Host ('[OK] ' + $r.Content); exit 0 } catch { Start-Sleep -Milliseconds 800 } }; Write-Host '[警告] 20 秒内无响应，请查看 logs\console.err.log'; exit 1"
if errorlevel 1 (
    echo [警告] 启动未成功，请检查 logs\console.err.log
    if not "%NO_PAUSE%"=="1" pause
    popd
    exit /b 1
)

echo.
echo 服务已在后台运行，关闭本窗口不影响。
echo   面板: http://127.0.0.1:%PORT%/dashboard
echo   日志: logs\proxy.log / logs\console.err.log
echo   停止: stop_proxy.bat %PORT%
echo.
if not "%NO_PAUSE%"=="1" pause
popd
