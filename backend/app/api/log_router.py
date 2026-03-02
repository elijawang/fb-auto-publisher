"""
日志管理 API 路由
"""
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.connection import get_session
from app.domain.log.log_service import LogService

router = APIRouter()


class LogResponse(BaseModel):
    id: str
    task_id: str
    account_name: str
    page_name: str
    video_file_name: str
    scheduled_time: Optional[str] = None
    actual_time: Optional[str] = None
    status: str
    error_message: str

    class Config:
        from_attributes = True


@router.get("/", response_model=List[LogResponse])
async def list_logs(
    task_id: Optional[str] = None,
    account_name: Optional[str] = None,
    page_name: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    """查询日志列表，支持多条件筛选"""
    service = LogService(session)
    logs = await service.list_logs(
        task_id=task_id,
        account_name=account_name,
        page_name=page_name,
        status=status,
        limit=limit,
        offset=offset,
    )
    result = []
    for log in logs:
        result.append(LogResponse(
            id=log.id,
            task_id=log.task_id,
            account_name=log.account_name,
            page_name=log.page_name,
            video_file_name=log.video_file_name,
            scheduled_time=log.scheduled_time.isoformat() if log.scheduled_time else None,
            actual_time=log.actual_time.isoformat() if log.actual_time else None,
            status=log.status,
            error_message=log.error_message,
        ))
    return result


@router.get("/summary/{task_id}", response_model=dict)
async def get_task_summary(task_id: str, session: AsyncSession = Depends(get_session)):
    """获取任务执行摘要统计"""
    service = LogService(session)
    return await service.get_task_summary(task_id)
