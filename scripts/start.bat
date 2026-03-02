@echo off
REM ========================================
REM Facebook自动化发布系统 - 启动脚本 (Windows)
REM ========================================

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..
set BACKEND_DIR=%PROJECT_DIR%\backend

echo 🚀 Facebook自动化发布系统 - 启动中...
echo 📂 项目目录: %PROJECT_DIR%

REM 检查Python
where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo ❌ 未找到python，请先安装Python 3.9+
    pause
    exit /b 1
)

python --version

REM 创建虚拟环境（如果不存在）
set VENV_DIR=%BACKEND_DIR%\.venv
if not exist "%VENV_DIR%" (
    echo 📦 创建虚拟环境...
    python -m venv "%VENV_DIR%"
)

REM 激活虚拟环境
call "%VENV_DIR%\Scripts\activate.bat"

REM 安装依赖
echo 📦 安装依赖...
pip install -r "%BACKEND_DIR%\requirements.txt" -q

REM 安装Playwright浏览器（首次运行）
if not exist "%LOCALAPPDATA%\ms-playwright" (
    echo 🌐 安装Playwright浏览器...
    python -m playwright install chromium
)

REM 启动后端服务
echo ✅ 启动后端服务: http://localhost:8000
echo 📖 API文档: http://localhost:8000/docs
cd /d "%BACKEND_DIR%"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

pause
