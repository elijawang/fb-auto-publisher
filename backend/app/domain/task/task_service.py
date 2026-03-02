"""
任务领域服务 - 任务创建、视频上传、调度管理
"""
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from loguru import logger

from app.infrastructure.config.settings import get_settings
from app.infrastructure.database.models import (
    PublishTask, TaskVideo, TaskStatus, TaskLog, VideoLogStatus, VideoStatus, FBPage, FBAccount,
)


class TaskService:
    """发布任务管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings = get_settings()

    async def create_task(
        self,
        account_id: str,
        task_name: str,
        description: str,
        start_time: datetime,
        interval_minutes: int = 60,
    ) -> PublishTask:
        """创建发布任务"""
        task = PublishTask(
            account_id=account_id,
            task_name=task_name,
            description=description,
            start_time=start_time,
            interval_minutes=interval_minutes,
            status=TaskStatus.DRAFT.value,
        )
        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        logger.info(f"创建任务: {task_name} | 起始时间: {start_time} | 间隔: {interval_minutes}分钟")
        return task

    async def add_video_to_task(
        self,
        task_id: str,
        file_name: str,
        source_path: str,
        sequence: int,
        page_id: str = "",
        page_name: str = "",
    ) -> TaskVideo:
        """
        添加视频到任务
        - 将视频文件复制到统一存储目录
        - 根据sequence和间隔自动计算发布时间
        - page_id/page_name 标识该视频子任务对应的公共主页
        """
        task = await self.get_task(task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")

        # 计算该视频的计划发布时间
        scheduled_time = task.start_time + timedelta(minutes=task.interval_minutes * (sequence - 1))

        # 复制视频到存储目录（跨平台Path操作）
        dest_dir = self.settings.video_dir / task_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / file_name

        source = Path(source_path)
        if source.exists():
            # 只在文件不存在时复制（多主页共用同一文件）
            if not dest_path.exists():
                shutil.copy2(str(source), str(dest_path))
            file_size = source.stat().st_size / (1024 * 1024)  # 转换为MB
        else:
            file_size = 0

        video = TaskVideo(
            task_id=task_id,
            file_name=file_name,
            file_path=str(dest_path),
            file_size=round(file_size, 2),
            sequence=sequence,
            scheduled_time=scheduled_time,
            page_id=page_id,
            page_name=page_name,
        )
        self.session.add(video)
        await self.session.commit()
        await self.session.refresh(video)
        logger.info(f"添加视频: {file_name} | 主页: {page_name} | 序号: {sequence} | 计划时间: {scheduled_time}")
        return video

    async def get_videos_by_page(
        self, task_id: str, page_name: str
    ) -> List[TaskVideo]:
        """获取任务下某个主页的所有视频子任务"""
        result = await self.session.execute(
            select(TaskVideo)
            .where(TaskVideo.task_id == task_id, TaskVideo.page_name == page_name)
            .order_by(TaskVideo.sequence)
        )
        return list(result.scalars().all())

    async def update_page_videos_status(
        self,
        task_id: str,
        page_name: str,
        status: VideoStatus,
        error_message: str = "",
    ) -> int:
        """批量更新任务下某个主页所有视频子任务的状态"""
        values = {"status": status.value}
        if error_message:
            values["error_message"] = error_message
        result = await self.session.execute(
            update(TaskVideo)
            .where(TaskVideo.task_id == task_id, TaskVideo.page_name == page_name)
            .values(**values)
        )
        await self.session.commit()
        logger.info(
            f"更新主页视频子任务状态: task_id={task_id}, page={page_name} -> {status.value}，"
            f"影响 {result.rowcount} 条"
        )
        return result.rowcount

    async def get_task(self, task_id: str) -> Optional[PublishTask]:
        """获取任务详情（含视频列表和日志）"""
        result = await self.session.execute(
            select(PublishTask)
            .options(selectinload(PublishTask.videos), selectinload(PublishTask.logs))
            .where(PublishTask.id == task_id)
        )
        return result.scalar_one_or_none()

    async def list_tasks(
        self,
        account_id: Optional[str] = None,
        status: Optional[str] = None,
        date_str: Optional[str] = None,
        group_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """
        获取任务列表（支持按天过滤和分页）
        
        Args:
            date_str: 日期字符串 "YYYY-MM-DD"，为空则不过滤
            page: 页码（从1开始）
            page_size: 每页条数
        
        Returns:
            {"items": [...], "total": int, "page": int, "page_size": int}
        """
        from sqlalchemy import func

        base_query = select(PublishTask).options(
            selectinload(PublishTask.videos),
            selectinload(PublishTask.logs),
        )
        count_query = select(func.count(PublishTask.id))

        # 过滤条件
        if account_id:
            base_query = base_query.where(PublishTask.account_id == account_id)
            count_query = count_query.where(PublishTask.account_id == account_id)
        if status:
            base_query = base_query.where(PublishTask.status == status)
            count_query = count_query.where(PublishTask.status == status)
        if group_id:
            # 通过关联FBAccount表按分组筛选
            base_query = base_query.join(FBAccount, PublishTask.account_id == FBAccount.id).where(FBAccount.group_id == group_id)
            count_query = count_query.join(FBAccount, PublishTask.account_id == FBAccount.id).where(FBAccount.group_id == group_id)
        if date_str:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d")
                day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
                day_end = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)
                base_query = base_query.where(
                    PublishTask.created_at >= day_start,
                    PublishTask.created_at <= day_end,
                )
                count_query = count_query.where(
                    PublishTask.created_at >= day_start,
                    PublishTask.created_at <= day_end,
                )
            except ValueError:
                logger.warning(f"无效的日期格式: {date_str}，忽略日期过滤")

        # 总数
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0

        # 分页
        offset = (page - 1) * page_size
        base_query = base_query.order_by(PublishTask.created_at.desc()).offset(offset).limit(page_size)
        result = await self.session.execute(base_query)
        items = list(result.scalars().all())

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def update_task_status(self, task_id: str, status: TaskStatus) -> bool:
        """更新任务状态"""
        result = await self.session.execute(
            update(PublishTask).where(PublishTask.id == task_id).values(status=status.value)
        )
        await self.session.commit()
        logger.info(f"更新任务状态: {task_id} -> {status.value}")
        return result.rowcount > 0

    async def delete_task(self, task_id: str) -> bool:
        """删除任务（级联删除关联视频和日志）"""
        task = await self.get_task(task_id)
        if not task:
            return False

        # 清理视频文件
        video_dir = self.settings.video_dir / task_id
        if video_dir.exists():
            shutil.rmtree(str(video_dir))

        await self.session.delete(task)
        await self.session.commit()
        logger.info(f"删除任务: {task.task_name}")
        return True

    async def update_video_status(
        self,
        video_id: str,
        status: VideoStatus,
        error_message: str = "",
    ) -> bool:
        """更新视频子任务状态"""
        values = {"status": status.value}
        if error_message:
            values["error_message"] = error_message
        result = await self.session.execute(
            update(TaskVideo).where(TaskVideo.id == video_id).values(**values)
        )
        await self.session.commit()
        logger.info(f"更新视频子任务状态: {video_id} -> {status.value}")
        return result.rowcount > 0

    async def update_all_videos_status(
        self,
        task_id: str,
        status: VideoStatus,
        error_message: str = "",
    ) -> int:
        """批量更新任务下所有视频子任务的状态"""
        values = {"status": status.value}
        if error_message:
            values["error_message"] = error_message
        result = await self.session.execute(
            update(TaskVideo).where(TaskVideo.task_id == task_id).values(**values)
        )
        await self.session.commit()
        logger.info(f"批量更新视频子任务状态: task_id={task_id} -> {status.value}，影响 {result.rowcount} 条")
        return result.rowcount

    async def finalize_task_status(self, task_id: str) -> TaskStatus:
        """
        根据视频子任务的状态自动推断并更新主任务状态。
        
        规则：
        - 所有视频子任务都是 PUBLISHED -> 主任务 COMPLETED
        - 所有视频子任务都是 FAILED -> 主任务 FAILED
        - 部分成功部分失败 -> 主任务 COMPLETED（但有部分失败的视频）
        - 有正在处理中的 -> 主任务 RUNNING
        """
        # 重新加载任务（确保获取最新的视频状态）
        result = await self.session.execute(
            select(PublishTask)
            .options(selectinload(PublishTask.videos))
            .where(PublishTask.id == task_id)
        )
        task = result.scalar_one_or_none()
        if not task or not task.videos:
            return TaskStatus.FAILED

        published = sum(1 for v in task.videos if v.status == VideoStatus.PUBLISHED.value)
        failed = sum(1 for v in task.videos if v.status == VideoStatus.FAILED.value)
        total = len(task.videos)

        if published == total:
            final_status = TaskStatus.COMPLETED
        elif failed == total:
            final_status = TaskStatus.FAILED
        elif published + failed == total:
            # 部分成功部分失败，仍标记为已完成
            final_status = TaskStatus.COMPLETED
        else:
            # 还有未完成的
            final_status = TaskStatus.RUNNING

        await self.update_task_status(task_id, final_status)
        logger.info(
            f"主任务最终状态: {final_status.value} "
            f"(成功: {published}/{total}, 失败: {failed}/{total})"
        )
        return final_status

    async def reset_page_videos_for_retry(
        self,
        task_id: str,
        page_name: str,
    ) -> List[TaskVideo]:
        """
        重置某个主页下所有失败/待发布的视频子任务状态为 pending，准备重新执行。
        返回被重置的视频子任务列表。
        """
        videos = await self.get_videos_by_page(task_id, page_name)
        reset_videos = []
        for v in videos:
            # 只重置非成功状态的视频
            if v.status != VideoStatus.PUBLISHED.value:
                await self.update_video_status(v.id, VideoStatus.PENDING)
                reset_videos.append(v)
        logger.info(
            f"重置主页视频子任务: task_id={task_id}, page={page_name}, "
            f"重置 {len(reset_videos)}/{len(videos)} 个"
        )
        return reset_videos

    async def get_task_schedule_preview(
        self, start_time: datetime, interval_minutes: int, video_count: int
    ) -> List[dict]:
        """预览发布时间安排"""
        preview = []
        for i in range(video_count):
            scheduled = start_time + timedelta(minutes=interval_minutes * i)
            preview.append({
                "sequence": i + 1,
                "scheduled_time": scheduled.isoformat(),
            })
        return preview
