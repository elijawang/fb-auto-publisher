"""
浏览器领域服务 - Chrome Profile管理与Playwright自动化
核心职责：
1. 管理Chrome Profile（创建/绑定/复用）
2. 自动登录Facebook
3. 身份认证暂停等待机制
4. 登录态检测
跨平台支持: Windows / macOS / Linux
"""
import asyncio
import platform
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.infrastructure.config.settings import get_settings
from app.infrastructure.database.models import BrowserProfile, FBAccount, AccountStatus
from app.infrastructure.encryption.cipher import decrypt_password


class BrowserManager:
    """
    浏览器实例管理器
    每个Facebook账号对应一个独立的Chrome Profile（分身）
    """

    # 人工认证轮询间隔（秒）
    AUTH_POLL_INTERVAL = 5
    # 人工认证默认超时时间（秒），默认10分钟
    AUTH_DEFAULT_TIMEOUT = 600

    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings = get_settings()
        # 存储活跃的浏览器上下文 {account_id: BrowserContext}
        self._active_contexts: Dict[str, BrowserContext] = {}
        self._playwright = None
        self._browser: Optional[Browser] = None
        # 存储正在等待人工认证的账号 {account_id: True/False}
        # 当用户在前端点击"认证完成"时，将对应account_id设置为True
        self._auth_confirmed: Dict[str, bool] = {}

    async def _ensure_playwright(self):
        """确保Playwright已初始化"""
        if self._playwright is None:
            self._playwright = await async_playwright().start()
            logger.info("Playwright引擎已启动")

    def _get_chrome_executable(self) -> Optional[str]:
        """
        获取Chrome可执行文件路径（跨平台）
        返回None时Playwright会自动使用内置Chromium
        """
        system = platform.system()
        candidates = []

        if system == "Windows":
            candidates = [
                Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
                Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
                Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
            ]
        elif system == "Darwin":  # macOS
            candidates = [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ]
        else:  # Linux
            candidates = [
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/google-chrome-stable"),
                Path("/usr/bin/chromium-browser"),
                Path("/usr/bin/chromium"),
            ]

        for path in candidates:
            if path.exists():
                logger.info(f"找到Chrome: {path}")
                return str(path)

        logger.warning("未找到系统Chrome，将使用Playwright内置Chromium")
        return None

    def _get_profile_path(self, profile_dir_name: str) -> Path:
        """获取Profile目录的完整路径"""
        return self.settings.profile_dir / profile_dir_name

    async def get_or_create_profile(self, account_id: str) -> BrowserProfile:
        """
        获取或创建浏览器Profile
        确保每个账号有且仅有一个绑定的Profile
        """
        result = await self.session.execute(
            select(BrowserProfile).where(BrowserProfile.account_id == account_id)
        )
        profile = result.scalar_one_or_none()

        if profile is None:
            profile_dir_name = f"profile_{account_id[:8]}"
            profile_path = self._get_profile_path(profile_dir_name)
            profile_path.mkdir(parents=True, exist_ok=True)

            profile = BrowserProfile(
                account_id=account_id,
                profile_dir_name=profile_dir_name,
            )
            self.session.add(profile)
            await self.session.commit()
            await self.session.refresh(profile)
            logger.info(f"创建浏览器Profile: {profile_dir_name} -> 账号ID: {account_id}")

        return profile

    async def launch_browser(self, account_id: str) -> BrowserContext:
        """
        启动浏览器（复用已有Profile）
        返回BrowserContext用于后续页面操作
        """
        # 如果已有活跃的上下文，直接返回
        if account_id in self._active_contexts:
            ctx = self._active_contexts[account_id]
            try:
                # 测试上下文是否仍然有效
                await ctx.pages[0].title() if ctx.pages else None
                return ctx
            except Exception:
                # 上下文已失效，移除
                del self._active_contexts[account_id]

        await self._ensure_playwright()

        profile = await self.get_or_create_profile(account_id)
        profile_path = self._get_profile_path(profile.profile_dir_name)

        chrome_path = self._get_chrome_executable()

        # 清理残留的SingletonLock文件，防止异常退出后无法启动
        self._clean_singleton_locks(profile_path)

        # 启动持久化上下文（Session/Cookie持久化）
        context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            executable_path=chrome_path,
            headless=self.settings.browser_headless,
            slow_mo=self.settings.browser_slow_mo,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            # 排除Playwright默认添加的一些参数，避免与Chrome Profile冲突和Chrome安全警告
            ignore_default_args=[
                "--enable-automation",
                "--disable-component-extensions-with-background-pages",
                "--no-sandbox",  # 避免Chrome警告"不受支持的命令行标记"
                "--disable-blink-features=AutomationControlled",  # 同上，Chrome不再支持此标记
            ],
            # 抑制弹窗 + Profile兼容性参数
            args=[
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                # 禁用可能导致"打开个人资料时出了点问题"弹窗的功能
                "--disable-features=TranslateUI,ChromeWhatsNewUI,PrivacySandboxSettings4,AutofillServerCommunication",
                "--disable-component-update",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-default-apps",
                "--disable-extensions",
                # 抑制崩溃恢复和错误弹窗
                "--disable-breakpad",
                "--disable-session-crashed-bubble",
                "--disable-domain-reliability",
                "--noerrdialogs",
                "--hide-crash-restore-bubble",
            ],
        )

        self._active_contexts[account_id] = context
        logger.info(f"浏览器已启动: 账号ID={account_id}, Profile={profile.profile_dir_name}")
        return context

    async def login_facebook(self, account_id: str, wait_for_auth: bool = False,
                              auth_timeout: int = None) -> dict:
        """
        登录Facebook
        
        参数:
            account_id: 账号ID
            wait_for_auth: 是否在需要人工认证时阻塞等待（True=等待人工操作后再返回，False=立即返回暂停状态）
            auth_timeout: 等待人工认证的超时时间（秒），默认使用 AUTH_DEFAULT_TIMEOUT
            
        返回: {"success": bool, "need_manual_auth": bool, "waiting_auth": bool, "message": str}
        """
        if auth_timeout is None:
            auth_timeout = self.AUTH_DEFAULT_TIMEOUT

        # 获取账号信息
        result = await self.session.execute(
            select(FBAccount).where(FBAccount.id == account_id)
        )
        account = result.scalar_one_or_none()
        if not account:
            return {"success": False, "need_manual_auth": False, "waiting_auth": False, "message": "账号不存在"}

        password = decrypt_password(account.password_encrypted)
        context = await self.launch_browser(account_id)

        # 获取或创建页面
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            # 访问Facebook
            await page.goto("https://www.facebook.com/", timeout=self.settings.page_load_timeout)

            # 等待页面完全加载，并处理可能出现的弹窗
            await self._dismiss_dialogs(page)
            await asyncio.sleep(2)

            # 检查是否已经登录
            if await self._check_logged_in(page):
                await self._update_login_status(account_id, True)
                return {"success": True, "need_manual_auth": False, "waiting_auth": False,
                        "message": "已登录状态，无需重复登录"}

            # 执行自动登录
            logger.info(f"开始自动登录: {account.email}")

            # 再次处理可能残留的弹窗
            await self._dismiss_dialogs(page)

            # 自动填充并提交登录表单
            await self._fill_and_submit_login(page, account.email, password)

            # 等待页面跳转
            await asyncio.sleep(3)
            await self._dismiss_dialogs(page)

            # 检查是否需要身份认证（2FA/验证码）
            if await self._check_needs_auth(page):
                # 更新账号状态为"等待人工认证中"
                await self._update_account_status(account_id, AccountStatus.WAITING_AUTH)
                logger.warning(f"账号 {account.email} 需要人工身份认证，浏览器已打开等待操作")

                if wait_for_auth:
                    # 【阻塞等待模式】：持续轮询直到用户完成认证或超时
                    return await self._wait_for_manual_auth(account_id, page, auth_timeout)
                else:
                    # 【非阻塞模式】：立即返回，由前端/用户主动调用 confirm_manual_auth
                    return {
                        "success": False,
                        "need_manual_auth": True,
                        "waiting_auth": False,
                        "message": "需要手动完成身份认证（2FA/验证码），请在弹出的浏览器中操作完成后，点击'认证完成'按钮继续",
                    }

            # 登录成功
            if await self._check_logged_in(page):
                await self._update_login_status(account_id, True)
                await self._update_account_status(account_id, AccountStatus.NORMAL)
                return {"success": True, "need_manual_auth": False, "waiting_auth": False, "message": "登录成功"}

            # 可能既没检测到认证页面，也没检测到登录成功
            # 这种情况也应该等待人工确认（可能是未识别的认证页面）
            logger.warning(f"账号 {account.email} 登录后状态不明，进入人工确认流程")
            await self._update_account_status(account_id, AccountStatus.WAITING_AUTH)

            if wait_for_auth:
                return await self._wait_for_manual_auth(account_id, page, auth_timeout)
            else:
                return {
                    "success": False,
                    "need_manual_auth": True,
                    "waiting_auth": False,
                    "message": "登录状态不明，请检查浏览器页面，手动完成操作后点击'认证完成'按钮",
                }

        except Exception as e:
            logger.error(f"登录过程出错: {e}")
            return {"success": False, "need_manual_auth": False, "waiting_auth": False,
                    "message": f"登录出错: {str(e)}"}

    async def confirm_manual_auth(self, account_id: str) -> dict:
        """
        用户手动完成认证后调用此方法确认。
        支持两种场景：
        1. 非阻塞模式下，用户在前端点击"认证完成"后调用
        2. 阻塞模式下，通知轮询循环用户已确认
        """
        if account_id not in self._active_contexts:
            return {"success": False, "message": "浏览器未启动"}

        context = self._active_contexts[account_id]
        page = context.pages[0] if context.pages else None
        if not page:
            return {"success": False, "message": "无可用页面"}

        # 标记用户已确认（通知轮询循环）
        self._auth_confirmed[account_id] = True

        if await self._check_logged_in(page):
            await self._update_login_status(account_id, True)
            await self._update_account_status(account_id, AccountStatus.NORMAL)
            self._auth_confirmed.pop(account_id, None)
            return {"success": True, "message": "认证完成，已成功登录"}

        return {"success": False, "message": "认证似乎尚未完成，请继续在浏览器中操作。如果您已完成了所有操作，请稍等片刻后再次点击确认。"}

    async def get_auth_status(self, account_id: str) -> dict:
        """
        获取账号的认证等待状态（供前端轮询查询）
        返回: {"waiting": bool, "logged_in": bool, "message": str}
        """
        if account_id not in self._active_contexts:
            return {"waiting": False, "logged_in": False, "message": "浏览器未启动"}

        context = self._active_contexts[account_id]
        page = context.pages[0] if context.pages else None
        if not page:
            return {"waiting": False, "logged_in": False, "message": "无可用页面"}

        is_logged_in = await self._check_logged_in(page)
        is_waiting = account_id in self._auth_confirmed or not is_logged_in

        return {
            "waiting": not is_logged_in,
            "logged_in": is_logged_in,
            "message": "已登录" if is_logged_in else "等待人工认证中，请在浏览器中完成操作",
        }

    async def close_browser(self, account_id: str):
        """关闭指定账号的浏览器实例"""
        if account_id in self._active_contexts:
            ctx = self._active_contexts.pop(account_id)
            try:
                await ctx.close()
            except Exception as e:
                logger.warning(f"关闭浏览器时出错: {e}")
            logger.info(f"浏览器已关闭: 账号ID={account_id}")

    async def close_all(self):
        """关闭所有浏览器实例"""
        for account_id in list(self._active_contexts.keys()):
            await self.close_browser(account_id)
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def get_page(self, account_id: str) -> Optional[Page]:
        """获取账号对应的活跃页面"""
        if account_id not in self._active_contexts:
            return None
        context = self._active_contexts[account_id]
        return context.pages[0] if context.pages else await context.new_page()

    async def navigate_to_url(self, account_id: str, url: str) -> dict:
        """
        在账号的浏览器中导航到指定URL。
        用于在前端点击"登录公共主页"时，直接在已打开的浏览器中跳转到对应主页。

        前置条件：浏览器已启动。
        """
        if account_id not in self._active_contexts:
            # 尝试启动浏览器
            await self.launch_browser(account_id)

        context = self._active_contexts.get(account_id)
        if not context:
            return {"success": False, "message": "浏览器未启动，请先登录账号"}

        page = context.pages[0] if context.pages else await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            logger.info(f"已导航到: {url} (account_id={account_id})")
            return {"success": True, "message": f"已跳转到: {url}"}
        except Exception as e:
            logger.error(f"导航到 {url} 失败: {e}")
            return {"success": False, "message": f"导航失败: {str(e)}"}

    # ==================== 内部方法 ====================

    async def _fill_and_submit_login(self, page: Page, email: str, password: str):
        """
        自动填充并提交Facebook登录表单。
        
        适配多种场景：
        1. 常规登录页 —— 标准的 email + pass 输入框在顶部
        2. 首次访问页（注册页）—— 注册表单占主区域，登录在顶部小表单中
        3. login页面 —— facebook.com/login 的独立登录页
        4. Cookie 弹窗遮挡 —— 先关闭弹窗再填充
        """
        # ============== 策略1: 尝试定位顶部登录区域（首次访问时的首页） ==============
        # 首次访问时，facebook.com 首页同时有注册表单和顶部登录表单
        # 注册表单也有 input[name="email"]，需要精准定位到登录区域的输入框
        login_form_selectors = [
            # 顶部登录表单区域（首页布局）—— form#login_form 或顶部导航区域
            'form#login_form input[name="email"]',
            # 标准登录页 facebook.com/login
            'form input[name="email"]',
            # 通用选择器
            'input[name="email"]',
        ]

        email_input = None
        for selector in login_form_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.is_visible(timeout=3000):
                    email_input = locator
                    logger.info(f"找到邮箱输入框: {selector}")
                    break
            except Exception:
                continue

        # 如果没找到，尝试导航到独立的登录页面
        if email_input is None:
            logger.warning("首页未找到邮箱输入框，尝试导航到 /login 页面")
            try:
                await page.goto("https://www.facebook.com/login/", timeout=self.settings.page_load_timeout)
                await self._dismiss_dialogs(page)
                await asyncio.sleep(2)

                for selector in login_form_selectors:
                    try:
                        locator = page.locator(selector).first
                        if await locator.is_visible(timeout=3000):
                            email_input = locator
                            logger.info(f"在/login页面找到邮箱输入框: {selector}")
                            break
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"导航到/login页面失败: {e}")

        # 如果仍然没找到，最后尝试刷新页面
        if email_input is None:
            logger.warning("邮箱输入框仍未找到，尝试刷新页面后重试")
            await self._dismiss_dialogs(page)
            await page.reload(timeout=self.settings.page_load_timeout)
            await self._dismiss_dialogs(page)
            await asyncio.sleep(2)

            email_input = page.locator('input[name="email"]').first
            await email_input.wait_for(state="visible", timeout=15000)

        # ============== 填充邮箱 ==============
        await email_input.click()
        await asyncio.sleep(0.3)
        # 先清空再填充，防止有默认值
        await email_input.fill("")
        await asyncio.sleep(0.1)
        await email_input.fill(email)
        logger.info(f"邮箱已填充: {email}")

        # ============== 填充密码 ==============
        # 密码框和邮箱框在同一个form中，用就近定位
        password_selectors = [
            'form#login_form input[name="pass"]',
            'form input[name="pass"]',
            'input[name="pass"]',
        ]

        password_input = None
        for selector in password_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.is_visible(timeout=3000):
                    password_input = locator
                    break
            except Exception:
                continue

        if password_input is None:
            # 某些Facebook布局中，密码框可能在点击"登录"后才出现
            logger.warning("密码输入框未找到，尝试先按Tab键或点击登录按钮触发密码框")
            await email_input.press("Tab")
            await asyncio.sleep(1)
            password_input = page.locator('input[name="pass"]').first
            await password_input.wait_for(state="visible", timeout=10000)

        await password_input.click()
        await asyncio.sleep(0.3)
        await password_input.fill("")
        await asyncio.sleep(0.1)
        await password_input.fill(password)
        logger.info("密码已填充")

        # ============== 点击登录按钮 ==============
        login_btn_selectors = [
            'form#login_form button[name="login"]',
            'form#login_form button[type="submit"]',
            'button[name="login"]',
            'button[data-testid="royal_login_button"]',
            'input[type="submit"][value="Log In"]',
            'input[type="submit"][value="登录"]',
            'button:has-text("Log In")',
            'button:has-text("登录")',
        ]

        for selector in login_btn_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    logger.info(f"登录按钮已点击: {selector}")
                    return
            except Exception:
                continue

        # 如果都没找到按钮，尝试在密码框按回车键提交
        logger.warning("未找到登录按钮，尝试在密码框中按回车键提交")
        await password_input.press("Enter")
        logger.info("已通过回车键提交登录表单")

    async def _dismiss_dialogs(self, page: Page, max_attempts: int = 10):
        """
        处理Chrome/Facebook可能弹出的各种对话框和弹窗。
        
        包括：
        - Chrome原生dialog（alert/confirm/prompt）
        - "打开您的个人资料时出了点问题"弹窗
        - Facebook Cookie同意弹窗
        - 其他干扰性弹窗
        
        参数:
            page: 当前页面
            max_attempts: 最大尝试次数，防止无限循环
        """
        # 注册一次性dialog处理器，自动dismiss Chrome原生弹窗
        async def handle_dialog(dialog):
            logger.info(f"自动关闭弹窗: type={dialog.type}, message={dialog.message[:100]}")
            await dialog.dismiss()

        # 先移除旧的listener避免重复注册
        try:
            page.remove_listener("dialog", handle_dialog)
        except Exception:
            pass
        page.on("dialog", handle_dialog)

        # 处理页面内的弹窗元素（非原生dialog）
        for attempt in range(max_attempts):
            dismissed = False
            try:
                # 通用关闭按钮选择器（覆盖各种弹窗的关闭方式）
                # 注意：选择器尽量限定在弹窗容器中，避免误点页面正常按钮
                close_selectors = [
                    # Facebook Cookie同意弹窗（优先处理，首次访问常见）
                    'button[data-cookiebanner="accept_button"]',
                    'button[data-cookiebanner="accept_only_essential_button"]',
                    'div[data-testid="cookie-policy-manage-dialog"] button:has-text("Allow")',
                    'div[data-testid="cookie-policy-manage-dialog"] button:has-text("接受")',
                    'div[data-testid="cookie-policy-manage-dialog"] button:has-text("允许")',
                    # dialog角色容器中的关闭按钮
                    'div[role="dialog"] button[aria-label="关闭"]',
                    'div[role="dialog"] button[aria-label="Close"]',
                    'div[role="dialog"] button:has-text("OK")',
                    'div[role="dialog"] button:has-text("确定")',
                    'div[role="dialog"] button:has-text("Got it")',
                    'div[role="dialog"] button:has-text("知道了")',
                    # Chrome原生弹窗的通用关闭按钮（非dialog角色时才用）
                    '[aria-label="关闭"]:not(form [aria-label="关闭"])',
                    '[aria-label="Close"]:not(form [aria-label="Close"])',
                ]

                for selector in close_selectors:
                    try:
                        btn = page.locator(selector).first
                        if await btn.is_visible(timeout=500):
                            await btn.click(timeout=2000)
                            dismissed = True
                            logger.info(f"已关闭弹窗元素: {selector}")
                            await asyncio.sleep(0.5)
                            break
                    except Exception:
                        continue

            except Exception as e:
                logger.debug(f"弹窗检测异常（可忽略）: {e}")

            if not dismissed:
                # 没有检测到更多弹窗，退出循环
                break

            await asyncio.sleep(0.3)

    async def _wait_for_manual_auth(self, account_id: str, page: Page,
                                     timeout: int) -> dict:
        """
        阻塞式等待人工认证完成。
        
        逻辑说明：
        - 浏览器已打开并停在认证页面，程序在此处暂停
        - 每隔 AUTH_POLL_INTERVAL 秒检测一次页面状态
        - 如果检测到已登录成功（用户在浏览器中手动完成了认证），立即返回成功
        - 如果用户通过前端点击了"认证完成"按钮（_auth_confirmed），也会触发检测
        - 超时后返回超时提示（不算失败，用户可以继续手动操作后重试）
        
        关键特性：
        - 不会按失败处理
        - 不会自动重试
        - 只是耐心等待，直到人工操作完成或超时
        """
        logger.info(f"进入人工认证等待模式: 账号ID={account_id}, 超时={timeout}秒")
        self._auth_confirmed.pop(account_id, None)  # 清除之前的确认标记

        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(self.AUTH_POLL_INTERVAL)
            elapsed += self.AUTH_POLL_INTERVAL

            try:
                # 检查方式1：页面自动检测（用户在浏览器中完成操作后，页面会自动跳转）
                if await self._check_logged_in(page):
                    logger.info(f"检测到登录成功（自动检测）: 账号ID={account_id}, 等待了{elapsed}秒")
                    await self._update_login_status(account_id, True)
                    await self._update_account_status(account_id, AccountStatus.NORMAL)
                    self._auth_confirmed.pop(account_id, None)
                    return {
                        "success": True,
                        "need_manual_auth": False,
                        "waiting_auth": False,
                        "message": f"认证成功，已登录（等待了{elapsed}秒）",
                    }

                # 检查方式2：用户在前端点击了"认证完成"按钮
                if self._auth_confirmed.get(account_id):
                    # 用户声称完成了，再检查一次
                    await asyncio.sleep(2)  # 给页面一点跳转时间
                    if await self._check_logged_in(page):
                        logger.info(f"用户确认认证完成，验证通过: 账号ID={account_id}")
                        await self._update_login_status(account_id, True)
                        await self._update_account_status(account_id, AccountStatus.NORMAL)
                        self._auth_confirmed.pop(account_id, None)
                        return {
                            "success": True,
                            "need_manual_auth": False,
                            "waiting_auth": False,
                            "message": "认证成功，已登录",
                        }
                    else:
                        # 用户点了确认但实际还没完成，继续等待
                        logger.info(f"用户点击了确认，但登录状态检测未通过，继续等待...")
                        self._auth_confirmed[account_id] = False

            except Exception as e:
                logger.warning(f"认证等待过程中检测出错: {e}，继续等待...")

            # 每30秒输出一次等待日志
            if elapsed % 30 == 0:
                logger.info(f"仍在等待人工认证: 账号ID={account_id}, 已等待{elapsed}秒/{timeout}秒")

        # 超时 - 不算失败，只是提示用户
        logger.warning(f"人工认证等待超时: 账号ID={account_id}, 已等待{timeout}秒")
        await self._update_account_status(account_id, AccountStatus.PENDING_AUTH)
        self._auth_confirmed.pop(account_id, None)
        return {
            "success": False,
            "need_manual_auth": True,
            "waiting_auth": True,
            "message": f"等待人工认证超时（{timeout}秒），请完成浏览器中的认证操作后，手动点击'认证完成'按钮，然后恢复任务",
        }

    def _clean_singleton_locks(self, profile_path: Path):
        """
        清理Chrome Profile目录下残留的SingletonLock/SingletonCookie/SingletonSocket文件。
        当Chrome进程异常退出（崩溃、强制终止）时，这些锁文件可能残留，
        导致下次启动时报错：
          Failed to create .../SingletonLock: File exists
          Aborting now to avoid profile corruption.
        此方法在每次启动浏览器前主动清理，确保启动成功。
        """
        singleton_files = ["SingletonLock", "SingletonCookie", "SingletonSocket"]
        for filename in singleton_files:
            lock_file = profile_path / filename
            if lock_file.exists() or lock_file.is_symlink():
                try:
                    lock_file.unlink()
                    logger.info(f"已清理残留锁文件: {lock_file}")
                except OSError as e:
                    logger.warning(f"清理锁文件失败: {lock_file}, 错误: {e}")

    async def _check_logged_in(self, page: Page) -> bool:
        """检查是否已登录Facebook（严格模式：必须检测到登录后的DOM元素）"""
        try:
            # Facebook登录后才会出现的特征性元素（必须至少匹配一个）
            logged_in_selectors = [
                '[aria-label="Facebook"]',
                '[aria-label="账号"]',
                '[aria-label="Account"]',
                '[aria-label="你的个人主页"]',
                '[aria-label="Your profile"]',
                'div[role="navigation"] a[aria-label]',
                '[data-pagelet="ProfileTilesFeed"]',
                # 首页Feed区域
                'div[role="feed"]',
                # 顶部搜索框（登录后才出现）
                'input[type="search"][aria-label]',
            ]
            for selector in logged_in_selectors:
                try:
                    if await page.locator(selector).count() > 0:
                        logger.debug(f"登录检测命中: {selector}")
                        return True
                except Exception:
                    continue

            # 注意：不再仅凭URL来判断登录状态
            # 因为很多未登录页面（2FA、验证码、错误页面等）URL中也不含 "login"
            return False
        except Exception:
            return False

    async def _check_needs_auth(self, page: Page) -> bool:
        """检查是否需要手动身份认证"""
        try:
            auth_indicators = [
                'input[name="approvals_code"]',  # 2FA验证码输入框
                '#checkpoint',                     # 安全检查页面
                'text="安全验证"',
                'text="两步验证"',
                'text="输入验证码"',
                'text="Enter the code"',
                'text="Two-factor authentication"',
            ]
            for selector in auth_indicators:
                if await page.locator(selector).count() > 0:
                    return True
            return False
        except Exception:
            return False

    async def _update_login_status(self, account_id: str, is_logged_in: bool):
        """更新浏览器Profile的登录状态"""
        await self.session.execute(
            update(BrowserProfile)
            .where(BrowserProfile.account_id == account_id)
            .values(is_logged_in=is_logged_in, last_login_at=datetime.utcnow())
        )
        await self.session.commit()

    async def _update_account_status(self, account_id: str, status: AccountStatus):
        """更新账号状态"""
        await self.session.execute(
            update(FBAccount).where(FBAccount.id == account_id).values(status=status.value)
        )
        await self.session.commit()

    async def fetch_pages(self, account_id: str) -> list:
        """
        通过浏览器抓取账号下的所有公共主页列表。

        前提：账号已登录（浏览器 session 有效）。
        返回格式：[{"name": str, "fb_id": str, "url": str}, ...]

        实现逻辑：
        1. 导航到 Meta Business Suite 首页
        2. 从页面解析所有可切换的公共主页信息
        3. 获取主页名称和 asset_id (fb_id)
        """
        context = self._active_contexts.get(account_id)
        if not context:
            # 尝试启动浏览器
            context = await self.launch_browser(account_id)

        page = context.pages[0] if context.pages else await context.new_page()

        pages_info = []

        try:
            # 1. 导航到 Meta Business Suite
            logger.info(f"正在导航到 Meta Business Suite 抓取主页列表... (account_id={account_id})")
            await page.goto("https://business.facebook.com/latest/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)

            # 获取当前页面 URL 中的 asset_id（即当前主页ID）
            current_url = page.url
            logger.info(f"Meta Business Suite 当前URL: {current_url}")

            # 2. 尝试多种方式获取主页列表

            # 方式1：通过 Meta Business Suite 的主页切换器（页面左上角的下拉菜单）
            fetched = await page.evaluate("""
                async () => {
                    const pages = [];
                    
                    // 尝试查找页面上的切换主页按钮/下拉菜单
                    // Meta Business Suite 通常在左上角有主页选择器
                    const switcherSelectors = [
                        '[data-testid="page-selector"]',
                        '[aria-label="Switch to a different page"]',
                        '[aria-label="Cambiar a una página diferente"]',
                        '[aria-label="切换到其他主页"]',
                    ];
                    
                    let switcherBtn = null;
                    for (const sel of switcherSelectors) {
                        switcherBtn = document.querySelector(sel);
                        if (switcherBtn) break;
                    }
                    
                    // 如果找不到切换器，尝试通过导航菜单中的账户信息
                    // 获取当前主页的名称和URL
                    const currentUrl = window.location.href;
                    const assetMatch = currentUrl.match(/asset_id=(\d+)/);
                    const currentAssetId = assetMatch ? assetMatch[1] : '';
                    
                    // 尝试获取页面标题中的主页名称
                    const titleEl = document.querySelector('title');
                    const pageTitle = titleEl ? titleEl.textContent : '';
                    
                    // 收集页面上所有可见的主页信息
                    // Meta Business Suite 的导航中通常会列出所有主页
                    const allLinks = document.querySelectorAll('a[href*="asset_id="], a[href*="/latest/"]');
                    const seenIds = new Set();
                    
                    for (const link of allLinks) {
                        const href = link.href || '';
                        const match = href.match(/asset_id=(\d+)/);
                        if (match) {
                            const fbId = match[1];
                            if (!seenIds.has(fbId)) {
                                seenIds.add(fbId);
                                const name = (link.textContent || '').trim();
                                if (name && name.length < 200) {
                                    pages.push({
                                        name: name,
                                        fb_id: fbId,
                                        url: `https://www.facebook.com/${fbId}`
                                    });
                                }
                            }
                        }
                    }
                    
                    // 如果当前有 asset_id 但没有在列表中，添加当前主页
                    if (currentAssetId && !seenIds.has(currentAssetId)) {
                        // 尝试获取当前主页名称
                        let currentPageName = '';
                        const nameSelectors = [
                            '[data-testid="page-name"]',
                            'h1', 'h2',
                            '[role="heading"]',
                        ];
                        for (const sel of nameSelectors) {
                            const el = document.querySelector(sel);
                            if (el && el.textContent.trim().length > 0 && el.textContent.trim().length < 100) {
                                currentPageName = el.textContent.trim();
                                break;
                            }
                        }
                        pages.push({
                            name: currentPageName || `Page_${currentAssetId}`,
                            fb_id: currentAssetId,
                            url: `https://www.facebook.com/${currentAssetId}`
                        });
                    }
                    
                    return { pages, switcherFound: !!switcherBtn, currentAssetId };
                }
            """)

            pages_info = fetched.get("pages", [])
            switcher_found = fetched.get("switcherFound", False)
            current_asset_id = fetched.get("currentAssetId", "")

            logger.info(
                f"方式1抓取结果: 找到 {len(pages_info)} 个主页, "
                f"主页切换器={'找到' if switcher_found else '未找到'}, "
                f"当前 asset_id={current_asset_id}"
            )

            # 方式2：如果找到主页切换器，点击它获取更多主页
            if switcher_found or len(pages_info) <= 1:
                try:
                    # 尝试点击主页切换器/账户图标来打开下拉列表
                    switcher_selectors = [
                        '[data-testid="page-selector"]',
                        '[aria-label="Switch to a different page"]',
                        '[aria-label="Cambiar a una página diferente"]',
                        'div[role="navigation"] img[alt]',
                    ]
                    for sel in switcher_selectors:
                        try:
                            btn = page.locator(sel).first
                            if await btn.is_visible(timeout=2000):
                                await btn.click()
                                await asyncio.sleep(2)
                                logger.info(f"点击了主页切换器: {sel}")

                                # 从下拉列表中抓取更多主页
                                more_pages = await page.evaluate("""
                                    () => {
                                        const pages = [];
                                        const seenIds = new Set();
                                        
                                        // 查找下拉菜单/弹出层中的主页选项
                                        const menuItems = document.querySelectorAll(
                                            '[role="menuitem"], [role="option"], [role="listbox"] [role="option"]'
                                        );
                                        
                                        for (const item of menuItems) {
                                            const text = (item.textContent || '').trim();
                                            const link = item.querySelector('a[href*="asset_id="]');
                                            let fbId = '';
                                            
                                            if (link) {
                                                const match = link.href.match(/asset_id=(\\d+)/);
                                                if (match) fbId = match[1];
                                            }
                                            
                                            if (text && text.length < 200 && !seenIds.has(text)) {
                                                seenIds.add(text);
                                                pages.push({
                                                    name: text,
                                                    fb_id: fbId,
                                                    url: fbId ? `https://www.facebook.com/${fbId}` : ''
                                                });
                                            }
                                        }
                                        
                                        // 也检查 listbox 中的选项
                                        const listItems = document.querySelectorAll('[role="listbox"] > *');
                                        for (const item of listItems) {
                                            const text = (item.textContent || '').trim();
                                            if (text && text.length < 200 && !seenIds.has(text)) {
                                                seenIds.add(text);
                                                const link = item.querySelector('a[href]');
                                                let fbId = '';
                                                if (link) {
                                                    const match = (link.href || '').match(/asset_id=(\\d+)/);
                                                    if (match) fbId = match[1];
                                                }
                                                pages.push({
                                                    name: text,
                                                    fb_id: fbId,
                                                    url: fbId ? `https://www.facebook.com/${fbId}` : ''
                                                });
                                            }
                                        }
                                        
                                        return pages;
                                    }
                                """)

                                if more_pages:
                                    # 合并结果，去重
                                    existing_ids = {p.get("fb_id") for p in pages_info if p.get("fb_id")}
                                    existing_names = {p.get("name") for p in pages_info}
                                    for mp in more_pages:
                                        if mp.get("fb_id") and mp["fb_id"] not in existing_ids:
                                            pages_info.append(mp)
                                            existing_ids.add(mp["fb_id"])
                                        elif mp.get("name") and mp["name"] not in existing_names:
                                            pages_info.append(mp)
                                            existing_names.add(mp["name"])
                                    logger.info(f"方式2补充抓取: 合并后共 {len(pages_info)} 个主页")

                                # 关闭下拉列表
                                await page.keyboard.press("Escape")
                                await asyncio.sleep(1)
                                break
                        except Exception:
                            continue
                except Exception as e:
                    logger.warning(f"方式2（点击切换器）抓取失败: {e}")

            # 方式3：导航到 Facebook "你的主页" 列表页
            # 在 https://www.facebook.com/pages/?category=your_pages 页面中
            # class 含有 x1n2onr6 的 div 是主页列表容器，其子 div 为各个公共主页
            if len(pages_info) <= 1:
                try:
                    logger.info("方式3：导航到 Facebook 你的主页列表页面抓取公共主页...")
                    await page.goto(
                        "https://www.facebook.com/pages/?category=your_pages",
                        wait_until="domcontentloaded", timeout=30000,
                    )
                    await asyncio.sleep(5)

                    # 等待 class 含 x1n2onr6 的容器出现
                    try:
                        await page.wait_for_selector("div.x1n2onr6", timeout=10000)
                        logger.info("方式3：找到 x1n2onr6 容器")
                    except Exception:
                        logger.warning("方式3：未找到 x1n2onr6 容器，尝试滚动加载...")
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(3)

                    your_pages = await page.evaluate("""
                        () => {
                            const pages = [];
                            const seenNames = new Set();
                            
                            // 查找 class 含有 x1n2onr6 的 div（公共主页列表容器）
                            const container = document.querySelector('div.x1n2onr6');
                            if (!container) return pages;
                            
                            // 遍历容器下的每个子 div，每个子 div 代表一个公共主页
                            const childDivs = container.querySelectorAll(':scope > div');
                            
                            for (const child of childDivs) {
                                // 获取主页名称：通常在链接文字或标题中
                                let name = '';
                                let url = '';
                                let fbId = '';
                                
                                // 查找主页的链接（<a>标签）
                                const links = child.querySelectorAll('a[href]');
                                for (const link of links) {
                                    const href = link.href || '';
                                    const text = (link.textContent || '').trim();
                                    
                                    // 过滤掉过长的文本或空文本（可能是整个div的文本）
                                    if (text && text.length > 1 && text.length < 150) {
                                        // 优先取包含 facebook.com 的链接
                                        if (href.includes('facebook.com/') && !href.includes('/pages/?')) {
                                            if (!name || text.length < name.length) {
                                                name = text;
                                            }
                                            if (!url) {
                                                url = href;
                                            }
                                        }
                                    }
                                    
                                    // 从链接中提取 fb_id
                                    if (href.includes('facebook.com/')) {
                                        // 匹配 /profile/123456 或 /pagename 或 /123456
                                        const idMatch = href.match(/facebook\\.com\\/(?:profile\\.php\\?id=|pages\\/[^/]+\\/|)(\\d+)/);
                                        if (idMatch && !fbId) {
                                            fbId = idMatch[1];
                                        }
                                        // 如果链接格式是 facebook.com/username
                                        if (!url || (!url.includes('facebook.com/') || url.includes('/pages/?'))) {
                                            url = href;
                                        }
                                    }
                                }
                                
                                // 如果没从链接获取到名称，尝试从子div内的文本获取
                                if (!name) {
                                    // 尝试获取第一个非空的文本节点
                                    const textEls = child.querySelectorAll('span, strong, h2, h3');
                                    for (const el of textEls) {
                                        const t = (el.textContent || '').trim();
                                        if (t && t.length > 1 && t.length < 150) {
                                            name = t;
                                            break;
                                        }
                                    }
                                }
                                
                                // 只添加有效的主页条目
                                if (name && !seenNames.has(name)) {
                                    seenNames.add(name);
                                    pages.push({
                                        name: name,
                                        fb_id: fbId,
                                        url: url || ''
                                    });
                                }
                            }
                            
                            return pages;
                        }
                    """)

                    logger.info(f"方式3抓取结果: 找到 {len(your_pages)} 个主页")
                    for yp in your_pages:
                        logger.info(f"  - {yp.get('name')} (fb_id={yp.get('fb_id', '?')}, url={yp.get('url', '?')})")

                    if your_pages:
                        existing_ids = {p.get("fb_id") for p in pages_info if p.get("fb_id")}
                        existing_names = {p.get("name") for p in pages_info}
                        for yp in your_pages:
                            if yp.get("fb_id") and yp["fb_id"] not in existing_ids:
                                pages_info.append(yp)
                                existing_ids.add(yp["fb_id"])
                            elif yp.get("name") and yp["name"] not in existing_names:
                                pages_info.append(yp)
                                existing_names.add(yp["name"])
                        logger.info(f"方式3补充抓取: 合并后共 {len(pages_info)} 个主页")

                except Exception as e:
                    logger.warning(f"方式3（Facebook主页列表）抓取失败: {e}")

            logger.info(
                f"公共主页抓取完成: account_id={account_id}, "
                f"共 {len(pages_info)} 个主页: "
                + ", ".join(f"{p.get('name')}({p.get('fb_id', '?')})" for p in pages_info)
            )

        except Exception as e:
            logger.error(f"抓取公共主页列表失败: {e}")
            raise

        return pages_info
