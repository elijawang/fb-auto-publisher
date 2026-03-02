@echo off
REM ========================================
REM Facebook自动化发布系统 - 停止脚本 (Windows)
REM ========================================

echo 🛑 Facebook自动化发布系统 - 正在停止...

REM 停止后端服务（uvicorn / python）
echo 🔸 正在查找后端进程...
for /f "tokens=2" %%i in ('tasklist /fi "imagename eq python.exe" /fo list ^| findstr "PID"') do (
    wmic process where "ProcessId=%%i" get CommandLine 2>nul | findstr /i "uvicorn" >nul
    if not errorlevel 1 (
        echo 🔸 终止后端进程 PID: %%i
        taskkill /PID %%i /F >nul 2>&1
    )
)

REM 通过端口号兜底终止（后端 8000，前端 3000）
echo 🔸 检查端口占用...

for %%P in (8000 3000) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%P " ^| findstr "LISTENING"') do (
        if not "%%a"=="0" (
            echo 🔸 端口 %%P 被 PID %%a 占用，正在终止...
            taskkill /PID %%a /F >nul 2>&1
            if not errorlevel 1 (
                echo ✅ 端口 %%P 已释放
            ) else (
                echo ⚠️  端口 %%P 释放失败，可能需要管理员权限
            )
        )
    )
)

REM 停止前端服务（node / vite）
for /f "tokens=2" %%i in ('tasklist /fi "imagename eq node.exe" /fo list ^| findstr "PID"') do (
    wmic process where "ProcessId=%%i" get CommandLine 2>nul | findstr /i "vite" >nul
    if not errorlevel 1 (
        echo 🔸 终止前端进程 PID: %%i
        taskkill /PID %%i /F >nul 2>&1
    )
)

echo.
echo 🏁 所有服务已停止
pause
