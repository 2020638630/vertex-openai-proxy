@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   vertex-openai-proxy 环境准备
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [X] 未找到 python，请先安装 Python 3.12+ 并加入 PATH
    echo     下载: https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python -V 2^>^&1') do set PYVER=%%v
echo [1/3] Python %PYVER%

echo [2/3] 安装依赖...
python -m pip install --upgrade pip >nul 2>nul
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [X] 依赖安装失败，请检查网络或代理设置
    pause
    exit /b 1
)

if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo [3/3] 已生成 .env
    echo       请打开 .env 填入 GEMINI_API_KEY，或配置服务账号（VERTEX_SA_JSON）
) else (
    echo [3/3] .env 已存在，跳过
)

echo.
echo 准备完成。
echo   启动服务: start_proxy.bat   或   python vertex_openai_proxy.py
echo   跑测试:   python -m pytest
echo.
pause
