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


@router.post("/retry-video/{video_id}", response_model=dict)
async def retry_video(
    video_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """
    重试失败的视频子任务。
    只允许重试状态为 failed 的视频子任务。
    会启动浏览器，仅对该视频所属的公共主页重新发布。
    """
    from app.domain.task.task_service import TaskService
    task_service = TaskService(session)

    # 查找该视频子任务
    from sqlalchemy import select
    from app.infrastructure.database.models import TaskVideo
    result = await session.execute(
        select(TaskVideo).where(TaskVideo.id == video_id)
    )
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="视频子任务不存在")

    if video.status != "failed":
        raise HTTPException(
            status_code=400,
            detail=f"只能重试失败的子任务，当前状态: {video.status}"
        )

    # 将该视频子任务状态重置为 pending
    from app.infrastructure.database.models import VideoStatus
    await task_service.update_video_status(video_id, VideoStatus.PENDING)

    # 重新更新主任务状态为 running
    from app.infrastructure.database.models import TaskStatus
    await task_service.update_task_status(video.task_id, TaskStatus.RUNNING)

    # 后台执行重试
    background_tasks.add_task(
        _run_retry_video_in_background, video.task_id, video_id, video.page_name
    )

    return {
        "message": f"视频 {video.file_name} 正在后台重试发布",
        "video_id": video_id,
        "status": "running",
    }


@router.post("/retry-page/{task_id}/{page_name}", response_model=dict)
async def retry_page_videos(
    task_id: str,
    page_name: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """
    重新执行某个公共主页下的所有子任务。
    将该主页下所有非成功的视频子任务重置为 pending 并重新发布。
    """
    from app.domain.task.task_service import TaskService
    task_service = TaskService(session)

    # 验证任务存在
    task = await task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 获取该主页下的视频子任务
    page_videos = await task_service.get_videos_by_page(task_id, page_name)
    if not page_videos:
        raise HTTPException(
            status_code=404,
            detail=f"任务下未找到主页 '{page_name}' 的视频子任务"
        )

    # 重置非成功的视频子任务状态为 pending
    reset_videos = await task_service.reset_page_videos_for_retry(task_id, page_name)
    if not reset_videos:
        return {
            "message": f"主页 '{page_name}' 下所有视频子任务均已成功，无需重试",
            "task_id": task_id,
            "page_name": page_name,
        }

    # 更新主任务状态为 running
    from app.infrastructure.database.models import TaskStatus
    await task_service.update_task_status(task_id, TaskStatus.RUNNING)

    # 后台执行
    video_ids = [v.id for v in reset_videos]
    background_tasks.add_task(
        _run_retry_page_in_background, task_id, page_name, video_ids
    )

    return {
        "message": f"主页 '{page_name}' 下 {len(reset_videos)} 个视频子任务正在后台重新执行",
        "task_id": task_id,
        "page_name": page_name,
        "retry_count": len(reset_videos),
        "status": "running",
    }


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


async def _run_retry_video_in_background(task_id: str, video_id: str, page_name: str):
    """后台执行重试单个视频子任务"""
    from app.infrastructure.database.connection import get_session as _get_session
    from loguru import logger

    try:
        async for session in _get_session():
            service = PublisherService(session)
            result = await service.retry_failed_videos(task_id, page_name, [video_id])
            logger.info(f"后台重试视频完成: video_id={video_id} -> {result}")
    except Exception as e:
        logger.error(f"后台重试视频异常: video_id={video_id} -> {e}")


async def _run_retry_page_in_background(task_id: str, page_name: str, video_ids: list):
    """后台执行重试整个公共主页的所有视频子任务"""
    from app.infrastructure.database.connection import get_session as _get_session
    from loguru import logger

    try:
        async for session in _get_session():
            service = PublisherService(session)
            result = await service.retry_failed_videos(task_id, page_name, video_ids)
            logger.info(f"后台重试主页视频完成: task_id={task_id}, page={page_name} -> {result}")
    except Exception as e:
        logger.error(f"后台重试主页视频异常: task_id={task_id}, page={page_name} -> {e}")
