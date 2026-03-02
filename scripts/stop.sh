#!/bin/bash
# ========================================
# Facebook自动化发布系统 - 停止脚本 (macOS/Linux)
# ========================================

echo "🛑 Facebook自动化发布系统 - 正在停止..."

# 停止后端服务（uvicorn）
BACKEND_PIDS=$(ps aux | grep '[u]vicorn app.main:app' | awk '{print $2}')
if [ -n "$BACKEND_PIDS" ]; then
    echo "🔸 发现后端进程: $BACKEND_PIDS"
    echo "$BACKEND_PIDS" | xargs kill -SIGTERM 2>/dev/null
    sleep 2
    # 如果SIGTERM未能终止，强制kill
    REMAINING=$(ps aux | grep '[u]vicorn app.main:app' | awk '{print $2}')
    if [ -n "$REMAINING" ]; then
        echo "⚠️  SIGTERM未生效，强制终止..."
        echo "$REMAINING" | xargs kill -9 2>/dev/null
    fi
    echo "✅ 后端服务已停止"
else
    echo "ℹ️  后端服务未在运行"
fi

# 停止前端服务（vite dev server）
FRONTEND_PIDS=$(ps aux | grep '[v]ite' | grep 'fb-auto-publisher' | awk '{print $2}')
if [ -n "$FRONTEND_PIDS" ]; then
    echo "🔸 发现前端进程: $FRONTEND_PIDS"
    echo "$FRONTEND_PIDS" | xargs kill -SIGTERM 2>/dev/null
    sleep 1
    REMAINING=$(ps aux | grep '[v]ite' | grep 'fb-auto-publisher' | awk '{print $2}')
    if [ -n "$REMAINING" ]; then
        echo "$REMAINING" | xargs kill -9 2>/dev/null
    fi
    echo "✅ 前端服务已停止"
else
    echo "ℹ️  前端服务未在运行"
fi

# 停止占用 8000 和 3000 端口的进程（兜底方案）
for PORT in 8000 3000; do
    PID=$(lsof -ti :$PORT 2>/dev/null)
    if [ -n "$PID" ]; then
        echo "🔸 端口 $PORT 仍被占用 (PID: $PID)，正在终止..."
        kill -SIGTERM $PID 2>/dev/null
        sleep 1
        # 再次检查
        PID=$(lsof -ti :$PORT 2>/dev/null)
        if [ -n "$PID" ]; then
            kill -9 $PID 2>/dev/null
        fi
        echo "✅ 端口 $PORT 已释放"
    fi
done

echo ""
echo "🏁 所有服务已停止"
