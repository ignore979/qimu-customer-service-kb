@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==========================================
echo   栖木家居客服知识库 正在启动
echo ==========================================
echo   浏览器将自动打开，若未打开请访问：
echo   http://127.0.0.1:8000
echo   停止服务：按 Ctrl+C
echo ==========================================
echo.

python app.py
if errorlevel 9009 goto nopython
pause
exit /b

:nopython
echo.
echo [错误] 未找到 Python 命令。
echo   请先安装 Python 3（安装时务必勾选 "Add python.exe to PATH"），
echo   然后重新双击本文件。
echo.
pause
