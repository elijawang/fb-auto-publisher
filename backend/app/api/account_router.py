"""
账号管理 API 路由
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.connection import get_session
from app.domain.account.account_service import AccountService
from app.domain.browser.browser_manager import BrowserManager

router = APIRouter()


# ==================== 请求/响应模型 ====================

class AccountCreate(BaseModel):
    email: str
    password: str
    name: str
    tags: str = ""
    profile_url: str = ""
    group_id: Optional[str] = None

class AccountUpdate(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None
    name: Optional[str] = None
    tags: Optional[str] = None
    status: Optional[str] = None
    profile_url: Optional[str] = None
    group_id: Optional[str] = None

class PageCreate(BaseModel):
    page_name: str
    page_url: str = ""
    page_fb_id: str = ""

class PageUpdate(BaseModel):
    page_name: Optional[str] = None
    page_url: Optional[str] = None
    page_fb_id: Optional[str] = None

class GroupCreate(BaseModel):
    name: str
    color: str = "#3498db"
    description: str = ""

class GroupUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None

class GroupResponse(BaseModel):
    id: str
    name: str
    color: str
    description: str
    account_count: int = 0

    class Config:
        from_attributes = True

class AccountResponse(BaseModel):
    id: str
    email: str
    name: str
    tags: str
    status: str
    profile_url: str = ""
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    group_color: Optional[str] = None
    pages_count: int = 0
    is_logged_in: bool = False

    class Config:
        from_attributes = True

class PageResponse(BaseModel):
    id: str
    account_id: str
    page_name: str
    page_url: str
    page_fb_id: str
    status: str
    fan_count: int

    class Config:
        from_attributes = True


# ==================== 分组管理路由 ====================

@router.post("/groups/", response_model=GroupResponse)
async def create_group(data: GroupCreate, session: AsyncSession = Depends(get_session)):
    """创建分组"""
    service = AccountService(session)
    group = await service.create_group(name=data.name, color=data.color, description=data.description)
    count = await service.get_group_account_count(group.id)
    return GroupResponse(id=group.id, name=group.name, color=group.color, description=group.description or "", account_count=count)


@router.get("/groups/", response_model=List[GroupResponse])
async def list_groups(session: AsyncSession = Depends(get_session)):
    """获取分组列表"""
    service = AccountService(session)
    groups = await service.list_groups()
    result = []
    for g in groups:
        count = await service.get_group_account_count(g.id)
        result.append(GroupResponse(id=g.id, name=g.name, color=g.color, description=g.description or "", account_count=count))
    return result


@router.get("/groups/{group_id}", response_model=GroupResponse)
async def get_group(group_id: str, session: AsyncSession = Depends(get_session)):
    """获取分组详情"""
    service = AccountService(session)
    group = await service.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="分组不存在")
    count = await service.get_group_account_count(group.id)
    return GroupResponse(id=group.id, name=group.name, color=group.color, description=group.description or "", account_count=count)


@router.put("/groups/{group_id}", response_model=dict)
async def update_group(group_id: str, data: GroupUpdate, session: AsyncSession = Depends(get_session)):
    """更新分组"""
    service = AccountService(session)
    group = await service.update_group(group_id, name=data.name, color=data.color, description=data.description)
    if not group:
        raise HTTPException(status_code=404, detail="分组不存在")
    return {"message": f"分组 {group.name} 更新成功"}


@router.delete("/groups/{group_id}", response_model=dict)
async def delete_group(group_id: str, session: AsyncSession = Depends(get_session)):
    """删除分组"""
    service = AccountService(session)
    success = await service.delete_group(group_id)
    if not success:
        raise HTTPException(status_code=404, detail="分组不存在")
    return {"message": "分组已删除"}


# ==================== 账号管理路由 ====================

@router.post("/", response_model=dict)
async def create_account(data: AccountCreate, session: AsyncSession = Depends(get_session)):
    """创建Facebook账号"""
    service = AccountService(session)
    account = await service.create_account(
        email=data.email, password=data.password, name=data.name,
        tags=data.tags, profile_url=data.profile_url, group_id=data.group_id,
    )
    return {"id": account.id, "message": f"账号 {account.name} 创建成功"}


@router.get("/", response_model=List[AccountResponse])
async def list_accounts(tag: Optional[str] = None, group_id: Optional[str] = None, session: AsyncSession = Depends(get_session)):
    """获取账号列表（支持按标签、分组筛选）"""
    service = AccountService(session)
    accounts = await service.list_accounts(tag=tag, group_id=group_id)
    result = []
    for acc in accounts:
        result.append(AccountResponse(
            id=acc.id,
            email=acc.email,
            name=acc.name,
            tags=acc.tags,
            status=acc.status,
            profile_url=acc.profile_url or "",
            group_id=acc.group_id,
            group_name=acc.group.name if acc.group else None,
            group_color=acc.group.color if acc.group else None,
            pages_count=len(acc.pages) if acc.pages else 0,
            is_logged_in=acc.browser_profile.is_logged_in if acc.browser_profile else False,
        ))
    return result


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(account_id: str, session: AsyncSession = Depends(get_session)):
    """获取账号详情"""
    service = AccountService(session)
    account = await service.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    return AccountResponse(
        id=account.id, email=account.email, name=account.name,
        tags=account.tags, status=account.status,
        profile_url=account.profile_url or "",
        group_id=account.group_id,
        group_name=account.group.name if account.group else None,
        group_color=account.group.color if account.group else None,
        pages_count=len(account.pages) if account.pages else 0,
        is_logged_in=account.browser_profile.is_logged_in if account.browser_profile else False,
    )


@router.put("/{account_id}", response_model=dict)
async def update_account(account_id: str, data: AccountUpdate, session: AsyncSession = Depends(get_session)):
    """更新账号信息"""
    service = AccountService(session)
    account = await service.update_account(
        account_id=account_id,
        email=data.email, password=data.password,
        name=data.name, tags=data.tags, status=data.status,
        profile_url=data.profile_url, group_id=data.group_id,
    )
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"message": f"账号 {account.name} 更新成功"}


@router.delete("/{account_id}", response_model=dict)
async def delete_account(account_id: str, session: AsyncSession = Depends(get_session)):
    """删除账号"""
    service = AccountService(session)
    success = await service.delete_account(account_id)
    if not success:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"message": "账号已删除"}


# ==================== 主页管理 ====================

@router.post("/{account_id}/pages", response_model=dict)
async def add_page(account_id: str, data: PageCreate, session: AsyncSession = Depends(get_session)):
    """为账号添加公共主页"""
    service = AccountService(session)
    page = await service.add_page(
        account_id=account_id,
        page_name=data.page_name,
        page_url=data.page_url,
        page_fb_id=data.page_fb_id,
    )
    return {"id": page.id, "message": f"主页 {page.page_name} 添加成功"}


@router.get("/{account_id}/pages", response_model=List[PageResponse])
async def list_pages(account_id: str, session: AsyncSession = Depends(get_session)):
    """获取账号下的所有公共主页"""
    service = AccountService(session)
    pages = await service.list_pages(account_id)
    return [PageResponse(
        id=p.id, account_id=p.account_id, page_name=p.page_name,
        page_url=p.page_url, page_fb_id=p.page_fb_id,
        status=p.status, fan_count=p.fan_count,
    ) for p in pages]


@router.delete("/pages/{page_id}", response_model=dict)
async def remove_page(page_id: str, session: AsyncSession = Depends(get_session)):
    """删除公共主页"""
    service = AccountService(session)
    success = await service.remove_page(page_id)
    if not success:
        raise HTTPException(status_code=404, detail="主页不存在")
    return {"message": "主页已删除"}


@router.put("/pages/{page_id}", response_model=dict)
async def update_page(
    page_id: str, data: PageUpdate, session: AsyncSession = Depends(get_session)
):
    """更新公共主页信息"""
    service = AccountService(session)
    page = await service.update_page(
        page_id=page_id,
        page_name=data.page_name,
        page_url=data.page_url,
        page_fb_id=data.page_fb_id,
    )
    if not page:
        raise HTTPException(status_code=404, detail="主页不存在")
    return {"message": f"主页 {page.page_name} 更新成功"}


@router.post("/{account_id}/pages/fetch", response_model=dict)
async def fetch_pages(
    account_id: str, session: AsyncSession = Depends(get_session)
):
    """
    通过浏览器自动抓取账号下的公共主页列表（名称 + ID）。
    
    前置条件：账号已登录（浏览器 session 有效）。
    抓取后自动同步到数据库（新增/更新已有主页）。
    """
    account_service = AccountService(session)
    account = await account_service.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    browser_manager = BrowserManager(session)

    try:
        # 抓取主页列表
        fetched_pages = await browser_manager.fetch_pages(account_id)
        if not fetched_pages:
            return {"message": "未抓取到公共主页，请确认账号已登录且拥有公共主页", "pages": []}

        # 同步到数据库
        sync_result = await account_service.sync_pages(account_id, fetched_pages)

        # 返回最新的主页列表
        pages = await account_service.list_pages(account_id)
        pages_data = [
            {
                "id": p.id, "page_name": p.page_name,
                "page_url": p.page_url, "page_fb_id": p.page_fb_id,
            }
            for p in pages
        ]

        return {
            "message": f"抓取完成: 新增 {sync_result['added']} 个，更新 {sync_result['updated']} 个，总计 {sync_result['total']} 个主页",
            "pages": pages_data,
            "sync_result": sync_result,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"抓取公共主页失败: {str(e)}"
        )
