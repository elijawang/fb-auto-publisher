#!/bin/bash
# ========================================
# Facebook自动化发布系统 - 启动脚本 (macOS/Linux)
# ========================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_DIR/backend"

echo "🚀 Facebook自动化发布系统 - 启动中..."
echo "📂 项目目录: $PROJECT_DIR"

# 检查Python
if ! command -v python3 &> /dev/null; then
    echo "❌ 未找到python3，请先安装Python 3.9+"
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "🐍 Python版本: $PYTHON_VERSION"

# 创建虚拟环境（如果不存在）
VENV_DIR="$BACKEND_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "📦 创建虚拟环境..."
    python3 -m venv "$VENV_DIR"
fi

# 激活虚拟环境
source "$VENV_DIR/bin/activate"

# 安装依赖
echo "📦 安装依赖..."
pip install -r "$BACKEND_DIR/requirements.txt" -q

# 安装Playwright浏览器（首次运行）
if [ ! -d "$HOME/.cache/ms-playwright" ] && [ ! -d "$HOME/Library/Caches/ms-playwright" ]; then
    echo "🌐 安装Playwright浏览器..."
    python3 -m playwright install chromium
fi

# 启动后端服务
echo "✅ 启动后端服务: http://localhost:8000"
echo "📖 API文档: http://localhost:8000/docs"
cd "$BACKEND_DIR"
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
