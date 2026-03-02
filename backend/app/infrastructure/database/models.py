"""
数据库模型定义 - SQLAlchemy ORM
核心数据表：账号、主页、任务、任务视频、任务日志、浏览器配置
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Text, ForeignKey, Enum
)
from sqlalchemy.orm import declarative_base, relationship
import enum

Base = declarative_base()


def generate_uuid() -> str:
    return str(uuid.uuid4())


# ==================== 枚举类型 ====================

class AccountStatus(str, enum.Enum):
    """账号状态"""
    NORMAL = "normal"           # 正常
    PENDING_AUTH = "pending_auth"  # 待认证（首次登录需人工认证）
    WAITING_AUTH = "waiting_auth"  # 等待人工认证中（浏览器已打开，等待用户操作）
    RESTRICTED = "restricted"   # 受限
    BANNED = "banned"           # 封禁


class TaskStatus(str, enum.Enum):
    """任务状态"""
    DRAFT = "draft"             # 草稿
    PENDING = "pending"         # 待执行
    RUNNING = "running"         # 执行中
    WAITING_AUTH = "waiting_auth"  # 等待人工认证中
    PAUSED = "paused"           # 已暂停
    COMPLETED = "completed"     # 已完成
    FAILED = "failed"           # 失败
    CANCELLED = "cancelled"     # 已取消


class VideoStatus(str, enum.Enum):
    """视频子任务状态"""
    PENDING = "pending"           # 待发布
    UPLOADING = "uploading"       # 上传中
    PROCESSING = "processing"     # 处理/版权检查中
    READY = "ready"               # 就绪（上传+检查完成，待发布）
    PUBLISHED = "published"       # 已发布
    FAILED = "failed"             # 失败


class VideoLogStatus(str, enum.Enum):
    """单个视频发布日志状态"""
    PENDING = "pending"         # 待发布
    UPLOADING = "uploading"     # 上传中
    PUBLISHED = "published"     # 已发布
    FAILED = "failed"           # 失败


# ==================== 数据表 ====================

class AccountGroup(Base):
    """账号分组表（如按语种分类）"""
    __tablename__ = "account_groups"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(100), nullable=False, unique=True, comment="分组名称（如：英语、日语、中文）")
    color = Column(String(20), default="#3498db", comment="分组颜色（十六进制色值）")
    description = Column(Text, default="", comment="分组描述")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联
    accounts = relationship("FBAccount", back_populates="group")


class FBAccount(Base):
    """Facebook账号表"""
    __tablename__ = "fb_accounts"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    email = Column(String(255), nullable=False, unique=True, comment="登录邮箱/手机号")
    password_encrypted = Column(Text, nullable=False, comment="加密后的密码")
    name = Column(String(100), nullable=False, comment="账号别名")
    profile_url = Column(String(500), default="", comment="账号个人主页链接")
    group_id = Column(String(36), ForeignKey("account_groups.id", ondelete="SET NULL"), nullable=True, comment="所属分组ID")
    tags = Column(String(500), default="", comment="标签，逗号分隔")
    status = Column(String(20), default=AccountStatus.PENDING_AUTH.value, comment="账号状态")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联
    group = relationship("AccountGroup", back_populates="accounts")
    pages = relationship("FBPage", back_populates="account", cascade="all, delete-orphan")
    browser_profile = relationship("BrowserProfile", back_populates="account", uselist=False,
                                   cascade="all, delete-orphan")
    tasks = relationship("PublishTask", back_populates="account", cascade="all, delete-orphan")


class FBPage(Base):
    """Facebook公共主页表"""
    __tablename__ = "fb_pages"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    account_id = Column(String(36), ForeignKey("fb_accounts.id"), nullable=False)
    page_name = Column(String(255), nullable=False, comment="主页名称")
    page_url = Column(String(500), default="", comment="主页链接")
    page_fb_id = Column(String(100), default="", comment="Facebook主页ID")
    status = Column(String(20), default="normal", comment="主页状态")
    fan_count = Column(Integer, default=0, comment="粉丝数量")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联
    account = relationship("FBAccount", back_populates="pages")


class BrowserProfile(Base):
    """浏览器Profile配置表（账号与Chrome分身绑定）"""
    __tablename__ = "browser_profiles"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    account_id = Column(String(36), ForeignKey("fb_accounts.id"), nullable=False, unique=True)
    profile_dir_name = Column(String(255), nullable=False, comment="Chrome Profile目录名")
    is_logged_in = Column(Boolean, default=False, comment="是否已登录")
    last_login_at = Column(DateTime, nullable=True, comment="最后登录时间")
    created_at = Column(DateTime, default=datetime.utcnow)

    # 关联
    account = relationship("FBAccount", back_populates="browser_profile")


class PublishTask(Base):
    """发布任务表"""
    __tablename__ = "publish_tasks"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    account_id = Column(String(36), ForeignKey("fb_accounts.id"), nullable=False)
    task_name = Column(String(255), nullable=False, comment="任务名称")
    description = Column(Text, default="", comment="视频统一描述文本")
    start_time = Column(DateTime, nullable=False, comment="起始发布时间")
    interval_minutes = Column(Integer, default=60, comment="发布间隔(分钟)")
    status = Column(String(20), default=TaskStatus.DRAFT.value, comment="任务状态")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联
    account = relationship("FBAccount", back_populates="tasks")
    videos = relationship("TaskVideo", back_populates="task", cascade="all, delete-orphan",
                          order_by="TaskVideo.sequence")
    logs = relationship("TaskLog", back_populates="task", cascade="all, delete-orphan")


class TaskVideo(Base):
    """任务关联的视频表（子任务）
    
    唯一键 = 视频名称 + 公共主页名称
    一个任务如果有 N 个视频 × M 个公共主页，则会生成 N×M 个视频子任务。
    """
    __tablename__ = "task_videos"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    task_id = Column(String(36), ForeignKey("publish_tasks.id"), nullable=False)
    file_name = Column(String(500), nullable=False, comment="原始文件名")
    file_path = Column(Text, nullable=False, comment="本地存储路径")
    file_size = Column(Float, default=0, comment="文件大小(MB)")
    sequence = Column(Integer, nullable=False, comment="发布顺序(从1开始)")
    scheduled_time = Column(DateTime, nullable=False, comment="计划发布时间")
    page_id = Column(String(36), default="", comment="关联的公共主页ID（FBPage.id）")
    page_name = Column(String(255), default="", comment="公共主页名称（冗余存储）")
    status = Column(String(20), default=VideoStatus.PENDING.value, comment="视频子任务状态")
    error_message = Column(Text, default="", comment="失败原因")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联
    task = relationship("PublishTask", back_populates="videos")


class TaskLog(Base):
    """任务执行日志表"""
    __tablename__ = "task_logs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    task_id = Column(String(36), ForeignKey("publish_tasks.id"), nullable=False)
    account_name = Column(String(100), default="", comment="账号名称")
    page_name = Column(String(255), default="", comment="主页名称")
    video_file_name = Column(String(500), default="", comment="视频文件名")
    scheduled_time = Column(DateTime, nullable=True, comment="计划发布时间")
    actual_time = Column(DateTime, nullable=True, comment="实际操作时间")
    status = Column(String(20), default=VideoLogStatus.PENDING.value, comment="状态")
    error_message = Column(Text, default="", comment="错误信息")
    created_at = Column(DateTime, default=datetime.utcnow)

    # 关联
    task = relationship("PublishTask", back_populates="logs")
