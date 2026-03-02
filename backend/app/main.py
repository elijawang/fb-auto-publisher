"""
Facebook自动化发布系统 - 主入口
支持 Windows / macOS / Linux
"""
import sys
import platform
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.infrastructure.database.connection import init_database, close_database
from app.infrastructure.config.settings import get_settings
from app.api.account_router import router as account_router
from app.api.task_router import router as task_router
from app.api.browser_router import router as browser_router
from app.api.publisher_router import router as publisher_router
from app.api.log_router import router as log_router


# 配置日志（跨平台兼容路径）
settings = get_settings()
logger.add(
    str(settings.log_file_path),
    rotation="10 MB",
    retention="30 days",
    encoding="utf-8",
    level="INFO",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info(f"系统启动 | OS: {platform.system()} {platform.release()} | Python: {sys.version}")
    logger.info(f"数据目录: {settings.data_dir}")
    await init_database()
    yield
    await close_database()
    logger.info("系统关闭")


app = FastAPI(
    title="Facebook自动化发布系统",
    description="支持多账号多主页的视频自动化发布与管理",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS中间件（允许前端跨域访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(account_router, prefix="/api/accounts", tags=["账号管理"])
app.include_router(task_router, prefix="/api/tasks", tags=["任务管理"])
app.include_router(browser_router, prefix="/api/browser", tags=["浏览器管理"])
app.include_router(publisher_router, prefix="/api/publisher", tags=["发布执行"])
app.include_router(log_router, prefix="/api/logs", tags=["日志管理"])


@app.get("/api/health")
async def health_check():
    """健康检查"""
    return {
        "status": "ok",
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "data_dir": str(settings.data_dir),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
