"""
日志领域服务 - 任务执行日志管理
"""
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.infrastructure.database.models import TaskLog, VideoLogStatus


class LogService:
    """任务日志管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_log(
        self,
        task_id: str,
        account_name: str = "",
        page_name: str = "",
        video_file_name: str = "",
        scheduled_time: datetime = None,
        status: VideoLogStatus = VideoLogStatus.PENDING,
        error_message: str = "",
    ) -> TaskLog:
        """创建执行日志"""
        log_entry = TaskLog(
            task_id=task_id,
            account_name=account_name,
            page_name=page_name,
            video_file_name=video_file_name,
            scheduled_time=scheduled_time,
            actual_time=datetime.utcnow(),
            status=status.value,
            error_message=error_message,
        )
        self.session.add(log_entry)
        await self.session.commit()
        await self.session.refresh(log_entry)
        return log_entry

    async def list_logs(
        self,
        task_id: Optional[str] = None,
        account_name: Optional[str] = None,
        page_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[TaskLog]:
        """查询日志列表，支持多条件筛选"""
        query = select(TaskLog)

        if task_id:
            query = query.where(TaskLog.task_id == task_id)
        if account_name:
            query = query.where(TaskLog.account_name.contains(account_name))
        if page_name:
            query = query.where(TaskLog.page_name.contains(page_name))
        if status:
            query = query.where(TaskLog.status == status)

        query = query.order_by(TaskLog.created_at.desc()).offset(offset).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_task_summary(self, task_id: str) -> dict:
        """获取任务执行摘要统计"""
        all_logs = await self.list_logs(task_id=task_id, limit=10000)

        total = len(all_logs)
        success = sum(1 for l in all_logs if l.status == VideoLogStatus.PUBLISHED.value)
        failed = sum(1 for l in all_logs if l.status == VideoLogStatus.FAILED.value)
        pending = sum(1 for l in all_logs if l.status == VideoLogStatus.PENDING.value)

        return {
            "task_id": task_id,
            "total": total,
            "published": success,
            "failed": failed,
            "pending": pending,
            "success_rate": round(success / total * 100, 1) if total > 0 else 0,
        }
