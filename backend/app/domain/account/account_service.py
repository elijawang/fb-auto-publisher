"""
账号领域服务 - 账号CRUD与管理
"""
from typing import List, Optional

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from loguru import logger

from app.infrastructure.database.models import FBAccount, FBPage, BrowserProfile, AccountGroup
from app.infrastructure.encryption.cipher import encrypt_password, decrypt_password


class AccountService:
    """Facebook账号管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ==================== 分组管理 ====================

    async def create_group(self, name: str, color: str = "#3498db", description: str = "") -> AccountGroup:
        """创建分组"""
        group = AccountGroup(name=name, color=color, description=description)
        self.session.add(group)
        await self.session.commit()
        await self.session.refresh(group)
        logger.info(f"创建分组: {name} (颜色: {color})")
        return group

    async def get_group(self, group_id: str) -> Optional[AccountGroup]:
        """根据ID获取分组"""
        result = await self.session.execute(
            select(AccountGroup).where(AccountGroup.id == group_id)
        )
        return result.scalar_one_or_none()

    async def list_groups(self) -> List[AccountGroup]:
        """获取所有分组（含账号数量）"""
        result = await self.session.execute(
            select(AccountGroup).order_by(AccountGroup.created_at)
        )
        return list(result.scalars().all())

    async def update_group(self, group_id: str, name: str = None, color: str = None,
                           description: str = None) -> Optional[AccountGroup]:
        """更新分组"""
        group = await self.get_group(group_id)
        if not group:
            return None
        if name is not None:
            group.name = name
        if color is not None:
            group.color = color
        if description is not None:
            group.description = description
        await self.session.commit()
        await self.session.refresh(group)
        logger.info(f"更新分组: {group.name}")
        return group

    async def delete_group(self, group_id: str) -> bool:
        """删除分组（分组下的账号将变为未分组状态）"""
        group = await self.get_group(group_id)
        if not group:
            return False
        await self.session.delete(group)
        await self.session.commit()
        logger.info(f"删除分组: {group.name}")
        return True

    async def get_group_account_count(self, group_id: str) -> int:
        """获取分组下的账号数量"""
        from sqlalchemy import func
        result = await self.session.execute(
            select(func.count(FBAccount.id)).where(FBAccount.group_id == group_id)
        )
        return result.scalar() or 0

    # ==================== 账号管理 ====================

    async def create_account(self, email: str, password: str, name: str,
                             tags: str = "", profile_url: str = "",
                             group_id: str = None) -> FBAccount:
        """创建新账号"""
        account = FBAccount(
            email=email,
            password_encrypted=encrypt_password(password),
            name=name,
            profile_url=profile_url,
            tags=tags,
            group_id=group_id if group_id else None,
        )
        self.session.add(account)
        await self.session.commit()
        await self.session.refresh(account)
        logger.info(f"创建账号成功: {name} ({email})")
        return account

    async def get_account(self, account_id: str) -> Optional[FBAccount]:
        """根据ID获取账号（含关联的主页、浏览器配置和分组）"""
        result = await self.session.execute(
            select(FBAccount)
            .options(
                selectinload(FBAccount.pages),
                selectinload(FBAccount.browser_profile),
                selectinload(FBAccount.group),
            )
            .where(FBAccount.id == account_id)
        )
        return result.scalar_one_or_none()

    async def list_accounts(self, tag: Optional[str] = None, group_id: Optional[str] = None) -> List[FBAccount]:
        """获取账号列表，可按标签或分组筛选"""
        query = select(FBAccount).options(
            selectinload(FBAccount.pages),
            selectinload(FBAccount.browser_profile),
            selectinload(FBAccount.group),
        )
        if tag:
            query = query.where(FBAccount.tags.contains(tag))
        if group_id:
            query = query.where(FBAccount.group_id == group_id)
        query = query.order_by(FBAccount.created_at.desc())
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def update_account(
        self, account_id: str, email: str = None, password: str = None,
        name: str = None, tags: str = None, status: str = None,
        profile_url: str = None, group_id: str = None,
    ) -> Optional[FBAccount]:
        """更新账号信息"""
        account = await self.get_account(account_id)
        if not account:
            return None
        if email is not None:
            account.email = email
        if password is not None:
            account.password_encrypted = encrypt_password(password)
        if name is not None:
            account.name = name
        if profile_url is not None:
            account.profile_url = profile_url
        if tags is not None:
            account.tags = tags
        if status is not None:
            account.status = status
        if group_id is not None:
            account.group_id = group_id if group_id else None
        await self.session.commit()
        await self.session.refresh(account)
        logger.info(f"更新账号: {account.name}")
        return account

    async def delete_account(self, account_id: str) -> bool:
        """删除账号（级联删除关联数据）"""
        account = await self.get_account(account_id)
        if not account:
            return False
        await self.session.delete(account)
        await self.session.commit()
        logger.info(f"删除账号: {account.name}")
        return True

    async def get_decrypted_password(self, account_id: str) -> Optional[str]:
        """获取解密后的密码（仅在自动登录时使用）"""
        account = await self.get_account(account_id)
        if not account:
            return None
        return decrypt_password(account.password_encrypted)

    async def add_page(self, account_id: str, page_name: str,
                       page_url: str = "", page_fb_id: str = "") -> FBPage:
        """为账号添加公共主页"""
        page = FBPage(
            account_id=account_id,
            page_name=page_name,
            page_url=page_url,
            page_fb_id=page_fb_id,
        )
        self.session.add(page)
        await self.session.commit()
        await self.session.refresh(page)
        logger.info(f"添加主页: {page_name} -> 账号ID: {account_id}")
        return page

    async def remove_page(self, page_id: str) -> bool:
        """删除公共主页"""
        result = await self.session.execute(
            delete(FBPage).where(FBPage.id == page_id)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def list_pages(self, account_id: str) -> List[FBPage]:
        """获取账号下的所有公共主页"""
        result = await self.session.execute(
            select(FBPage).where(FBPage.account_id == account_id).order_by(FBPage.created_at)
        )
        return list(result.scalars().all())

    async def update_page(self, page_id: str, page_name: str = None,
                          page_url: str = None, page_fb_id: str = None) -> Optional[FBPage]:
        """更新公共主页信息"""
        result = await self.session.execute(
            select(FBPage).where(FBPage.id == page_id)
        )
        page = result.scalar_one_or_none()
        if not page:
            return None
        if page_name is not None:
            page.page_name = page_name
        if page_url is not None:
            page.page_url = page_url
        if page_fb_id is not None:
            page.page_fb_id = page_fb_id
        await self.session.commit()
        await self.session.refresh(page)
        logger.info(f"更新主页: {page.page_name} (fb_id={page.page_fb_id})")
        return page

    async def sync_pages(self, account_id: str, fetched_pages: list) -> dict:
        """
        同步抓取到的公共主页列表到数据库。
        
        逻辑：
        - 按 page_fb_id 匹配已有主页，更新名称
        - 如无匹配，按 page_name 匹配
        - 新主页则创建
        
        Args:
            account_id: 账号ID
            fetched_pages: 从 Facebook 抓取的主页列表 [{"name": str, "fb_id": str, "url": str}, ...]
        
        Returns:
            {"added": int, "updated": int, "total": int}
        """
        existing_pages = await self.list_pages(account_id)

        # 构建已有主页索引
        by_fb_id = {p.page_fb_id: p for p in existing_pages if p.page_fb_id}
        by_name = {p.page_name: p for p in existing_pages}

        added = 0
        updated = 0

        for fp in fetched_pages:
            name = fp.get("name", "")
            fb_id = fp.get("fb_id", "")
            url = fp.get("url", "")

            if not name:
                continue

            # 先按 fb_id 匹配
            existing = by_fb_id.get(fb_id) if fb_id else None
            # 再按 name 匹配
            if not existing:
                existing = by_name.get(name)

            if existing:
                # 更新已有主页信息
                need_update = False
                if fb_id and existing.page_fb_id != fb_id:
                    existing.page_fb_id = fb_id
                    need_update = True
                if url and existing.page_url != url:
                    existing.page_url = url
                    need_update = True
                if name and existing.page_name != name:
                    existing.page_name = name
                    need_update = True
                if need_update:
                    updated += 1
            else:
                # 新增主页
                new_page = FBPage(
                    account_id=account_id,
                    page_name=name,
                    page_url=url,
                    page_fb_id=fb_id,
                )
                self.session.add(new_page)
                added += 1

        await self.session.commit()
        total = len(existing_pages) + added
        logger.info(
            f"同步主页完成: 账号ID={account_id}, 新增={added}, 更新={updated}, 总计={total}"
        )
        return {"added": added, "updated": updated, "total": total}
