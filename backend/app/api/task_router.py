"""
任务管理 API 路由
"""
import os
import shutil
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.config.settings import get_settings
from app.infrastructure.database.connection import get_session
from app.domain.task.task_service import TaskService
from app.domain.account.account_service import AccountService
from app.infrastructure.database.models import FBAccount, FBPage
from sqlalchemy import select
from sqlalchemy.orm import selectinload

router = APIRouter()


# ==================== 请求/响应模型 ====================

class TaskCreate(BaseModel):
    account_id: str
    task_name: str
    description: str
    start_time: datetime
    interval_minutes: int = 60

class SchedulePreview(BaseModel):
    start_time: datetime
    interval_minutes: int
    video_count: int

class TaskResponse(BaseModel):
    id: str
    account_id: str
    account_name: str = ""
    account_profile_url: str = ""
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    group_color: Optional[str] = None
    task_name: str
    description: str
    start_time: str
    interval_minutes: int
    status: str
    video_count: int = 0

    class Config:
        from_attributes = True

class VideoResponse(BaseModel):
    id: str
    task_id: str
    file_name: str
    file_size: float
    sequence: int
    scheduled_time: str
    page_id: str = ""
    page_name: str = ""
    page_url: str = ""
    status: str = "pending"
    error_message: str = ""

    class Config:
        from_attributes = True


# ==================== 路由 ====================

@router.post("/", response_model=dict)
async def create_task(data: TaskCreate, session: AsyncSession = Depends(get_session)):
    """创建发布任务"""
    service = TaskService(session)
    task = await service.create_task(
        account_id=data.account_id,
        task_name=data.task_name,
        description=data.description,
        start_time=data.start_time,
        interval_minutes=data.interval_minutes,
    )
    return {"id": task.id, "message": f"任务 {task.task_name} 创建成功"}


@router.post("/{task_id}/videos/upload", response_model=dict)
async def upload_videos(
    task_id: str,
    files: List[UploadFile] = File(...),
    session: AsyncSession = Depends(get_session),
):
    """
    批量上传视频文件到任务。
    
    会自动为账号下的每个公共主页创建子任务：
    N个视频 × M个主页 = N×M 个视频子任务
    """
    settings = get_settings()
    task_service = TaskService(session)
    account_service = AccountService(session)

    # 验证任务存在
    task = await task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 获取账号下的所有公共主页
    pages = await account_service.list_pages(task.account_id)
    if not pages:
        raise HTTPException(
            status_code=400,
            detail="该账号下没有公共主页，请先在账号管理中添加或抓取公共主页"
        )

    # 创建临时存储目录
    temp_dir = settings.video_dir / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # 先保存所有视频到临时目录
    temp_files = []
    for file in files:
        temp_path = temp_dir / file.filename
        with open(str(temp_path), "wb") as f:
            content = await file.read()
            f.write(content)
        temp_files.append({"filename": file.filename, "temp_path": str(temp_path)})

    # 为每个 视频×主页 组合创建子任务
    existing_count = len(task.videos) if task.videos else 0
    uploaded = []
    seq = existing_count
    for page in pages:
        for index, tf in enumerate(temp_files, start=1):
            seq += 1
            video = await task_service.add_video_to_task(
                task_id=task_id,
                file_name=tf["filename"],
                source_path=tf["temp_path"],
                sequence=seq,
                page_id=page.id,
                page_name=page.page_name,
            )
            uploaded.append({
                "file_name": tf["filename"],
                "page_name": page.page_name,
                "sequence": video.sequence,
            })

    # 清理临时文件
    for tf in temp_files:
        p = settings.video_dir / "temp" / tf["filename"]
        if p.exists():
            os.remove(str(p))

    return {
        "message": (
            f"成功上传 {len(temp_files)} 个视频，"
            f"关联 {len(pages)} 个公共主页，"
            f"共创建 {len(uploaded)} 个视频子任务"
        ),
        "videos": uploaded,
    }


class TaskListResponse(BaseModel):
    items: List[TaskResponse]
    total: int
    page: int
    page_size: int


@router.get("/", response_model=TaskListResponse)
async def list_tasks(
    account_id: Optional[str] = None,
    status: Optional[str] = None,
    date: Optional[str] = None,
    group_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    session: AsyncSession = Depends(get_session),
):
    """
    获取任务列表（含账号信息）
    
    支持按天查询、按分组查询和分页：
    - date: 日期字符串 "YYYY-MM-DD"，默认当天
    - group_id: 分组ID，按账号分组筛选任务
    - page: 页码（从1开始）
    - page_size: 每页条数，默认20
    """
    from datetime import date as date_type

    # 如果未传入日期，默认当天
    if not date:
        date = date_type.today().isoformat()

    service = TaskService(session)
    result = await service.list_tasks(
        account_id=account_id,
        status=status,
        date_str=date,
        group_id=group_id,
        page=page,
        page_size=page_size,
    )
    tasks = result["items"]

    # 批量获取所有相关账号信息（含分组）
    account_ids = list(set(t.account_id for t in tasks))
    account_map = {}
    if account_ids:
        from app.infrastructure.database.models import AccountGroup
        acc_result = await session.execute(
            select(FBAccount)
            .options(selectinload(FBAccount.pages), selectinload(FBAccount.group))
            .where(FBAccount.id.in_(account_ids))
        )
        for acc in acc_result.scalars().all():
            account_map[acc.id] = acc

    items = [TaskResponse(
        id=t.id, account_id=t.account_id,
        account_name=account_map.get(t.account_id, None) and account_map[t.account_id].name or "",
        account_profile_url=account_map.get(t.account_id, None) and account_map[t.account_id].profile_url or "",
        group_id=account_map.get(t.account_id, None) and account_map[t.account_id].group_id or None,
        group_name=(account_map.get(t.account_id, None) and account_map[t.account_id].group and account_map[t.account_id].group.name) or None,
        group_color=(account_map.get(t.account_id, None) and account_map[t.account_id].group and account_map[t.account_id].group.color) or None,
        task_name=t.task_name,
        description=t.description, start_time=t.start_time.isoformat(),
        interval_minutes=t.interval_minutes, status=t.status,
        video_count=len(t.videos) if t.videos else 0,
    ) for t in tasks]

    return TaskListResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.get("/{task_id}", response_model=dict)
async def get_task(task_id: str, session: AsyncSession = Depends(get_session)):
    """获取任务详情（含账号信息和公共主页链接）"""
    service = TaskService(session)
    task = await service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 获取账号信息及其公共主页和分组
    account_name = ""
    account_profile_url = ""
    group_name = None
    group_color = None
    page_url_map = {}  # page_name -> page_url 映射
    acc_result = await session.execute(
        select(FBAccount)
        .options(selectinload(FBAccount.pages), selectinload(FBAccount.group))
        .where(FBAccount.id == task.account_id)
    )
    account = acc_result.scalar_one_or_none()
    if account:
        account_name = account.name or ""
        account_profile_url = account.profile_url or ""
        if account.group:
            group_name = account.group.name
            group_color = account.group.color
        for p in (account.pages or []):
            page_url_map[p.page_name] = p.page_url or ""

    videos = [VideoResponse(
        id=v.id, task_id=v.task_id, file_name=v.file_name,
        file_size=v.file_size, sequence=v.sequence,
        scheduled_time=v.scheduled_time.isoformat(),
        page_id=v.page_id or "",
        page_name=v.page_name or "",
        page_url=page_url_map.get(v.page_name, ""),
        status=v.status or "pending",
        error_message=v.error_message or "",
    ) for v in (task.videos or [])]

    return {
        "id": task.id,
        "account_id": task.account_id,
        "account_name": account_name,
        "account_profile_url": account_profile_url,
        "group_name": group_name,
        "group_color": group_color,
        "task_name": task.task_name,
        "description": task.description,
        "start_time": task.start_time.isoformat(),
        "interval_minutes": task.interval_minutes,
        "status": task.status,
        "videos": [v.model_dump() for v in videos],
    }


@router.delete("/{task_id}", response_model=dict)
async def delete_task(task_id: str, session: AsyncSession = Depends(get_session)):
    """删除任务"""
    service = TaskService(session)
    success = await service.delete_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"message": "任务已删除"}


@router.post("/schedule-preview", response_model=List[dict])
async def schedule_preview(data: SchedulePreview, session: AsyncSession = Depends(get_session)):
    """预览发布时间安排"""
    service = TaskService(session)
    return await service.get_task_schedule_preview(
        start_time=data.start_time,
        interval_minutes=data.interval_minutes,
        video_count=data.video_count,
    )
