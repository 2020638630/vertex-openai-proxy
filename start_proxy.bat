@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [提示] 已根据模板创建 .env
    echo        请用记事本打开 .env，把 GEMINI_API_KEY= 后面填上 AIza... 开头的 Key
    echo.
    notepad .env
    pause
    exit /b 1
)

findstr /r /c:"^GEMINI_API_KEY=..*" ".env" >nul
if errorlevel 1 (
    echo [错误] .env 里的 GEMINI_API_KEY 还是空的，请先填写后再运行
    notepad .env
    pause
    exit /b 1
)

echo ================================================
echo  Vertex AI - OpenAI 兼容代理
echo  Base URL : http://127.0.0.1:4000/v1
echo  API Key  : sk-vertex-local  (随便填)
echo  模型      : gemini-2.5-pro
echo  关闭窗口即停止服务
echo ================================================
echo.

netstat -ano | findstr ":4000" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [提示] 4000 端口已在监听，服务可能已经在运行
    echo        先访问 http://127.0.0.1:4000/ 确认，如需重启请先结束旧进程
    pause
    exit /b 0
)

python vertex_openai_proxy.py
pause
