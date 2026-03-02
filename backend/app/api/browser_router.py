"""
浏览器管理 API 路由
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.connection import get_session
from app.domain.browser.browser_manager import BrowserManager

router = APIRouter()


class LoginRequest(BaseModel):
    """登录请求参数"""
    wait_for_auth: bool = False  # 是否阻塞等待人工认证（True=等待，False=立即返回）
    auth_timeout: Optional[int] = None  # 等待人工认证的超时时间（秒），默认600秒


@router.post("/launch/{account_id}", response_model=dict)
async def launch_browser(account_id: str, session: AsyncSession = Depends(get_session)):
    """启动账号对应的浏览器实例"""
    manager = BrowserManager(session)
    try:
        await manager.launch_browser(account_id)
        return {"message": "浏览器已启动"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"启动浏览器失败: {str(e)}")


@router.post("/login/{account_id}", response_model=dict)
async def login_facebook(
    account_id: str,
    request: LoginRequest = LoginRequest(),
    session: AsyncSession = Depends(get_session),
):
    """
    登录Facebook
    
    - 自动填写账密，触发2FA时根据wait_for_auth参数决定行为：
      - wait_for_auth=False（默认）：立即返回need_manual_auth=True，由前端提示用户操作
      - wait_for_auth=True：程序阻塞等待，持续轮询检测登录状态，直到用户完成认证或超时
    """
    manager = BrowserManager(session)
    result = await manager.login_facebook(
        account_id,
        wait_for_auth=request.wait_for_auth,
        auth_timeout=request.auth_timeout,
    )
    return result


@router.post("/confirm-auth/{account_id}", response_model=dict)
async def confirm_manual_auth(account_id: str, session: AsyncSession = Depends(get_session)):
    """
    确认手动认证完成（用户在浏览器中完成2FA后调用）
    
    当login接口返回need_manual_auth=True时，用户在浏览器中手动完成身份认证操作后，
    调用此接口通知后端认证已完成。
    """
    manager = BrowserManager(session)
    result = await manager.confirm_manual_auth(account_id)
    return result


@router.get("/auth-status/{account_id}", response_model=dict)
async def get_auth_status(account_id: str, session: AsyncSession = Depends(get_session)):
    """
    查询账号的认证等待状态（供前端轮询）
    
    返回:
    - waiting: 是否仍在等待认证
    - logged_in: 是否已登录成功
    - message: 状态描述
    """
    manager = BrowserManager(session)
    result = await manager.get_auth_status(account_id)
    return result


@router.post("/close/{account_id}", response_model=dict)
async def close_browser(account_id: str, session: AsyncSession = Depends(get_session)):
    """关闭账号对应的浏览器实例"""
    manager = BrowserManager(session)
    await manager.close_browser(account_id)
    return {"message": "浏览器已关闭"}


@router.post("/close-all", response_model=dict)
async def close_all_browsers(session: AsyncSession = Depends(get_session)):
    """关闭所有浏览器实例"""
    manager = BrowserManager(session)
    await manager.close_all()
    return {"message": "所有浏览器已关闭"}


@router.post("/navigate-page/{account_id}", response_model=dict)
async def navigate_to_page(
    account_id: str,
    page_url: str = Query(..., description="要导航到的公共主页链接"),
    session: AsyncSession = Depends(get_session),
):
    """
    在账号的浏览器中导航到指定的公共主页链接。
    前置条件：账号浏览器已启动并登录。
    """
    manager = BrowserManager(session)
    try:
        result = await manager.navigate_to_url(account_id, page_url)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导航失败: {str(e)}")
