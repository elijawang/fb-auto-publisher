"""
数据库连接管理 - 异步SQLAlchemy
"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from loguru import logger

from app.infrastructure.config.settings import get_settings
from app.infrastructure.database.models import Base

_engine = None
_session_factory = None


async def init_database():
    """初始化数据库（创建引擎和表）"""
    global _engine, _session_factory
    settings = get_settings()
    db_url = f"sqlite+aiosqlite:///{settings.db_path}"
    logger.info(f"初始化数据库: {settings.db_path}")

    _engine = create_async_engine(db_url, echo=settings.debug)
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    # 自动创建所有表
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 自动迁移：为已存在的表添加缺失的列
    # SQLite 的 create_all 不会修改已存在的表结构，需要手动 ALTER TABLE
    async with _engine.begin() as conn:
        await conn.run_sync(_migrate_tables)

    logger.info("数据库初始化完成")


def _migrate_tables(connection):
    """
    检查并添加缺失的列（兼容旧版数据库）。
    SQLite 不支持 ALTER TABLE DROP COLUMN / MODIFY COLUMN，
    但支持 ALTER TABLE ADD COLUMN。
    """
    import sqlalchemy as sa

    inspector = sa.inspect(connection)

    # 迁移 fb_accounts 表
    if "fb_accounts" in inspector.get_table_names():
        existing_columns = {col["name"] for col in inspector.get_columns("fb_accounts")}
        fb_account_migrations = [
            ("profile_url", "VARCHAR(500) DEFAULT ''"),
        ]
        for col_name, col_type in fb_account_migrations:
            if col_name not in existing_columns:
                try:
                    connection.execute(
                        sa.text(f"ALTER TABLE fb_accounts ADD COLUMN {col_name} {col_type}")
                    )
                    logger.info(f"已添加列: fb_accounts.{col_name} ({col_type})")
                except Exception as e:
                    logger.warning(f"添加列 fb_accounts.{col_name} 失败（可能已存在）: {e}")

    # 获取 task_videos 表的现有列
    if "task_videos" in inspector.get_table_names():
        existing_columns = {col["name"] for col in inspector.get_columns("task_videos")}

        # 添加缺失的列
        migrations = [
            ("status", "VARCHAR(20) DEFAULT 'pending'"),
            ("error_message", "TEXT DEFAULT ''"),
            ("updated_at", "DATETIME"),
            ("page_id", "VARCHAR(36) DEFAULT ''"),
            ("page_name", "VARCHAR(255) DEFAULT ''"),
        ]
        for col_name, col_type in migrations:
            if col_name not in existing_columns:
                try:
                    connection.execute(
                        sa.text(f"ALTER TABLE task_videos ADD COLUMN {col_name} {col_type}")
                    )
                    logger.info(f"已添加列: task_videos.{col_name} ({col_type})")
                except Exception as e:
                    logger.warning(f"添加列 task_videos.{col_name} 失败（可能已存在）: {e}")


async def close_database():
    """关闭数据库连接"""
    global _engine
    if _engine:
        await _engine.dispose()
        logger.info("数据库连接已关闭")


async def get_session() -> AsyncSession:
    """获取数据库会话（用于依赖注入）"""
    if _session_factory is None:
        await init_database()
    async with _session_factory() as session:
        yield session
