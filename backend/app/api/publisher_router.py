"""
发布执行 API 路由
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.connection import get_session
from app.domain.publisher.publisher_service import PublisherService

router = APIRouter()


@router.post("/execute/{task_id}", response_model=dict)
async def execute_task(
    task_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """
    执行发布任务（异步后台执行）
    任务会在后台运行，可通过日志接口查看进度
    """
    service = PublisherService(session)

    # 先做基本校验
    task = await service.task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 在后台执行（不阻塞API响应）
    # 注意：实际生产中可能需要独立的进程/线程来管理浏览器
    background_tasks.add_task(_run_task_in_background, task_id)

    return {
        "message": f"任务 {task.task_name} 已开始在后台执行",
        "task_id": task_id,
        "status": "running",
    }


@router.post("/resume/{task_id}", response_model=dict)
async def resume_task(task_id: str, session: AsyncSession = Depends(get_session)):
    """恢复暂停的任务（手动认证完成后调用）"""
    service = PublisherService(session)
    result = await service.resume_task(task_id)
    return result


async def _run_task_in_background(task_id: str):
    """后台执行任务的异步包装"""
    from app.infrastructure.database.connection import get_session as _get_session
    from loguru import logger

    try:
        # 需要新建独立的数据库会话
        async for session in _get_session():
            service = PublisherService(session)
            result = await service.execute_task(task_id)
            logger.info(f"后台任务完成: {task_id} -> {result}")
    except Exception as e:
        logger.error(f"后台任务异常: {task_id} -> {e}")
