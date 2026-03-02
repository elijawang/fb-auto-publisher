"""
全局配置管理 - 使用pathlib确保跨平台兼容
支持: Windows / macOS / Linux
"""
import platform
from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings


def get_project_root() -> Path:
    """获取项目根目录（跨平台）"""
    return Path(__file__).resolve().parent.parent.parent.parent


def get_data_dir() -> Path:
    """获取数据存储目录（跨平台兼容 Windows/macOS/Linux）"""
    system = platform.system()
    if system == "Windows":
        base = Path.home() / "AppData" / "Local" / "FBAutoPublisher"
    elif system == "Darwin":  # macOS
        base = Path.home() / "Library" / "Application Support" / "FBAutoPublisher"
    else:  # Linux及其他
        base = Path.home() / ".fb-auto-publisher"
    base.mkdir(parents=True, exist_ok=True)
    return base


class Settings(BaseSettings):
    """应用配置"""

    # 应用信息
    app_name: str = "Facebook自动化发布系统"
    debug: bool = False

    # 数据目录（跨平台）
    project_root: Path = get_project_root()
    data_dir: Path = get_data_dir()

    # 数据库
    db_path: Path = get_data_dir() / "fb_publisher.db"

    # 视频存储目录
    video_dir: Path = get_data_dir() / "videos"

    # Chrome Profile存储目录
    profile_dir: Path = get_data_dir() / "profiles"

    # 日志目录
    log_dir: Path = get_data_dir() / "logs"
    log_file_path: Path = get_data_dir() / "logs" / "app.log"

    # 加密密钥（生产环境应从环境变量读取）
    encryption_key: str = "fb-auto-publisher-default-key-32b"

    # Playwright配置
    browser_headless: bool = False  # 默认有头模式（需要用户手动认证）
    browser_slow_mo: int = 100  # 操作间隔(ms)，模拟人类操作节奏
    page_load_timeout: int = 60000  # 页面加载超时(ms)
    upload_timeout: int = 300000  # 视频上传超时(ms) = 5分钟

    # 发布配置
    max_retry: int = 3  # 最大重试次数
    retry_delay: int = 5  # 重试间隔(秒)

    class Config:
        env_prefix = "FB_"  # 环境变量前缀，如 FB_DEBUG=true
        env_file = ".env"

    def ensure_dirs(self):
        """确保所有必要目录存在"""
        for dir_path in [self.data_dir, self.video_dir, self.profile_dir, self.log_dir]:
            dir_path.mkdir(parents=True, exist_ok=True)


@lru_cache()
def get_settings() -> Settings:
    """获取全局配置单例"""
    settings = Settings()
    settings.ensure_dirs()
    return settings
