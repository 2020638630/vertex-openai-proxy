@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] GET /v1/models
curl -s http://127.0.0.1:4000/v1/models
echo.
echo.

echo [2/3] POST /v1/chat/completions  (非流式)
curl -s -X POST http://127.0.0.1:4000/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"gemini-2.5-pro\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: PROXY OK\"}]}"
echo.
echo.

echo [3/3] POST /v1/chat/completions  (流式 SSE)
curl -s -N -X POST http://127.0.0.1:4000/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"gemini-2.5-pro\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Count 1 to 5, one number per line\"}]}"
echo.
echo.
echo === 测试结束 ===
pause
