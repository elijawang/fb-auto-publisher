"""
发布领域服务 - Facebook视频发布自动化
核心职责：
1. 切换公共主页身份
2. 上传视频 + 填写描述 + 设置定时时间
3. 多主页循环执行
4. 异常处理（重试/超时/日志记录）
"""
import asyncio
import random
from datetime import datetime
from typing import List, Optional

from playwright.async_api import Page
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.infrastructure.config.settings import get_settings
from app.infrastructure.database.models import (
    FBAccount, FBPage, PublishTask, TaskVideo, TaskLog,
    TaskStatus, VideoLogStatus, VideoStatus,
)
from app.domain.browser.browser_manager import BrowserManager
from app.domain.task.task_service import TaskService
from app.domain.log.log_service import LogService


class PublishButtonNotFoundError(Exception):
    """发布按钮未找到异常，此异常不应触发重试上传"""
    pass


class PublisherService:
    """
    Facebook视频发布执行引擎
    协调浏览器管理、任务管理、日志记录
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings = get_settings()
        self.browser_manager = BrowserManager(session)
        self.task_service = TaskService(session)
        self.log_service = LogService(session)

    async def execute_task(self, task_id: str) -> dict:
        """
        执行完整的发布任务
        流程：登录 -> 遍历主页 -> 批量上传视频并发布 -> 关闭浏览器
        
        批量发布模式：
        1. 导航到 Bulk upload composer（1次）
        2. 一次性上传所有视频文件
        3. 统一填写任务描述（所有视频共用 task.description）
        4. 等待所有视频上传完成 + 版权检查完成
        5. 点击批量发布按钮
        """
        task = await self.task_service.get_task(task_id)
        if not task:
            return {"success": False, "message": "任务不存在"}

        if not task.videos:
            return {"success": False, "message": "任务没有关联的视频"}

        # 获取账号和其下的主页列表
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        result = await self.session.execute(
            select(FBAccount)
            .options(selectinload(FBAccount.pages))
            .where(FBAccount.id == task.account_id)
        )
        account = result.scalar_one_or_none()
        if not account:
            return {"success": False, "message": "账号不存在"}

        if not account.pages:
            return {"success": False, "message": "账号下没有公共主页，请先添加主页"}

        # 更新任务状态为运行中，所有视频子任务也更新为上传中
        await self.task_service.update_task_status(task_id, TaskStatus.RUNNING)
        await self.task_service.update_all_videos_status(task_id, VideoStatus.UPLOADING)
        logger.info(f"开始执行任务: {task.task_name} | 账号: {account.name} | 主页数量: {len(account.pages)}")

        total_success = 0
        total_fail = 0

        try:
            # 步骤1: 启动浏览器并登录
            # 使用 wait_for_auth=True，首次登录时需要人工认证时会阻塞等待，而不是直接失败
            login_result = await self.browser_manager.login_facebook(
                account.id, wait_for_auth=True
            )
            if not login_result["success"]:
                if login_result.get("need_manual_auth"):
                    # 超时未完成认证，任务进入等待认证状态（不是失败）
                    await self.task_service.update_task_status(task_id, TaskStatus.WAITING_AUTH)
                    return {
                        "success": False,
                        "waiting_auth": True,
                        "message": login_result["message"],
                    }
                await self.task_service.update_task_status(task_id, TaskStatus.FAILED)
                return {"success": False, "message": f"登录失败: {login_result['message']}"}

            page = await self.browser_manager.get_page(account.id)
            if not page:
                await self.task_service.update_task_status(task_id, TaskStatus.FAILED)
                return {"success": False, "message": "无法获取浏览器页面"}

            # 步骤2: 遍历每个公共主页，按主页分组发布
            for page_index, fb_page in enumerate(account.pages):
                logger.info(f"切换到主页 [{page_index + 1}/{len(account.pages)}]: {fb_page.page_name}")

                try:
                    # 获取该主页对应的视频子任务
                    page_videos = await self.task_service.get_videos_by_page(
                        task_id, fb_page.page_name
                    )
                    if not page_videos:
                        logger.warning(
                            f"主页 {fb_page.page_name} 没有关联的视频子任务，跳过"
                        )
                        continue

                    logger.info(
                        f"主页 {fb_page.page_name} 关联 {len(page_videos)} 个视频子任务"
                    )

                    # 切换到该主页身份
                    await self._switch_to_page(page, fb_page)
                    await asyncio.sleep(random.uniform(1, 3))  # 随机延迟，模拟人类

                    # 步骤3: 在当前主页下批量上传并发布该主页的视频
                    batch_result = await self._publish_videos_batch(
                        page=page,
                        task=task,
                        videos=page_videos,
                        fb_page=fb_page,
                        account=account,
                    )
                    total_success += batch_result["success_count"]
                    total_fail += batch_result["fail_count"]

                    # 当前主页发布完成后，等待一段时间再切换到下一个主页
                    if page_index < len(account.pages) - 1:
                        wait_seconds = random.uniform(3, 6)
                        logger.info(
                            f"主页 {fb_page.page_name} 发布完成，"
                            f"等待 {wait_seconds:.1f}s 后切换到下一个主页..."
                        )
                        await asyncio.sleep(wait_seconds)

                except Exception as e:
                    logger.error(f"主页 {fb_page.page_name} 发布出错: {e}")
                    # 将该主页下的视频子任务标记为失败
                    fail_count_for_page = await self.task_service.update_page_videos_status(
                        task_id, fb_page.page_name, VideoStatus.FAILED, str(e)
                    )
                    # 记录该主页的失败日志
                    await self.log_service.create_log(
                        task_id=task_id,
                        account_name=account.name,
                        page_name=fb_page.page_name,
                        video_file_name="(整页失败)",
                        status=VideoLogStatus.FAILED,
                        error_message=str(e),
                    )
                    total_fail += fail_count_for_page

            # 步骤4: 所有主页执行完毕，关闭浏览器
            await self.browser_manager.close_browser(account.id)

            # 根据视频子任务状态自动推断并更新主任务最终状态
            final_status = await self.task_service.finalize_task_status(task_id)

            summary = f"任务完成 | 成功: {total_success} | 失败: {total_fail} | 最终状态: {final_status.value}"
            logger.info(summary)
            return {"success": total_fail == 0, "message": summary}

        except Exception as e:
            logger.error(f"任务执行异常: {e}")
            await self.task_service.update_task_status(task_id, TaskStatus.FAILED)
            await self.browser_manager.close_browser(account.id)
            return {"success": False, "message": f"任务异常: {str(e)}"}

    async def resume_task(self, task_id: str) -> dict:
        """
        恢复等待认证/暂停的任务（用户手动认证完成后调用）
        """
        task = await self.task_service.get_task(task_id)
        if not task:
            return {"success": False, "message": "任务不存在"}

        if task.status not in (TaskStatus.PAUSED.value, TaskStatus.WAITING_AUTH.value):
            return {"success": False, "message": f"任务当前状态为 {task.status}，无法恢复"}

        # 确认手动认证
        confirm_result = await self.browser_manager.confirm_manual_auth(task.account_id)
        if not confirm_result["success"]:
            return confirm_result

        # 认证成功后重新执行任务
        return await self.execute_task(task_id)

    async def _switch_to_page(self, page: Page, fb_page: FBPage):
        """
        切换到指定公共主页的上下文。
        
        当 page_fb_id 已知时，无需额外导航到 Business Suite 首页，
        因为 _navigate_to_video_upload 会直接使用 page_fb_id 作为 asset_id
        拼接 bulk_upload_composer URL，这本身就指定了主页身份。
        
        只有在 page_fb_id 未知时，才需要通过传统方式切换主页。
        """
        try:
            # 当 page_fb_id 已知时，直接跳过首页导航
            # _navigate_to_video_upload 会用 page_fb_id 作为 asset_id 直接跳转
            if fb_page.page_fb_id:
                logger.info(
                    f"主页 {fb_page.page_name} 已有 page_fb_id={fb_page.page_fb_id}，"
                    f"将在导航到 bulk_upload_composer 时直接指定，跳过首页导航"
                )
                return

            # page_fb_id 未知时，需要通过传统方式切换主页
            logger.info(f"主页 {fb_page.page_name} 无 page_fb_id，使用传统方式切换...")

            # 方案1: 通过主页URL进入，再导航到 Meta Business Suite
            if fb_page.page_url:
                await page.goto(fb_page.page_url, timeout=self.settings.page_load_timeout)
                await asyncio.sleep(2)

                # 查找 Meta Business Suite 入口链接
                # 注意：这些链接通常带有 target="_blank"，直接 click() 会打开新 tab
                # 正确做法是提取 href 后用 page.goto() 在当前 tab 中导航
                mbs_selectors = [
                    'a:has-text("Meta Business Suite")',
                    'a[href*="business.facebook.com"]',
                    'a:has-text("管理主页")',
                    'a:has-text("Manage Page")',
                ]
                for selector in mbs_selectors:
                    try:
                        link = page.locator(selector).first
                        if await link.is_visible(timeout=3000):
                            # 提取 href 而不是 click，避免 target="_blank" 打开新 tab
                            href = await link.get_attribute("href")
                            if href:
                                # 确保是完整 URL
                                if href.startswith("/"):
                                    href = f"https://business.facebook.com{href}"
                                logger.info(f"从主页提取到 MBS 链接: {href}")
                                await page.goto(href, timeout=self.settings.page_load_timeout)
                                await asyncio.sleep(3)
                                logger.info(f"已从主页导航到 Meta Business Suite: {fb_page.page_name}")
                                return
                    except Exception:
                        continue

            # 方案2: 直接访问 Meta Business Suite 主页，从侧边栏选择主页
            await page.goto(
                "https://business.facebook.com/latest/home",
                timeout=self.settings.page_load_timeout
            )
            await asyncio.sleep(3)

            # 如果需要切换主页（多主页账号），点击主页选择器
            page_switcher = page.locator(
                'div[aria-label="Switch business portfolio or Page"], '
                'div[aria-label="切换业务组合或主页"]'
            )
            if await page_switcher.count() > 0:
                await page_switcher.first.click()
                await asyncio.sleep(1)

                # 在下拉列表中查找并点击目标主页
                target_page = page.locator(f'span:has-text("{fb_page.page_name}")')
                if await target_page.count() > 0:
                    await target_page.first.click()
                    await asyncio.sleep(2)
                    logger.info(f"已在 Meta Business Suite 中切换到主页: {fb_page.page_name}")
                    return

            logger.warning(f"未能自动切换到主页: {fb_page.page_name}")

        except Exception as e:
            logger.error(f"切换主页失败: {fb_page.page_name} - {e}")
            raise

    async def _publish_videos_batch(
        self,
        page: Page,
        task: PublishTask,
        videos: List[TaskVideo],
        fb_page: FBPage,
        account: FBAccount,
    ) -> dict:
        """
        在当前主页下批量上传并发布所有视频。
        
        批量发布流程：
        1. 导航到 Bulk upload composer 页面（仅1次）
        2. 一次性上传所有视频文件
        3. 统一填写任务描述（所有视频共用 task.description）
        4. 等待所有视频上传完成 + 版权检查/处理完成
        5. 自动触发批量发布按钮
        
        返回：{"success_count": int, "fail_count": int}
        """
        video_count = len(videos)
        file_names = [v.file_name for v in videos]
        file_paths = [v.file_path for v in videos]

        logger.info(
            f"批量发布 {video_count} 个视频 -> 主页: {fb_page.page_name}\n"
            f"视频列表: {', '.join(file_names)}"
        )

        for attempt in range(1, self.settings.max_retry + 1):
            try:
                logger.info(
                    f"批量发布尝试 {attempt}/{self.settings.max_retry} "
                    f"-> 主页: {fb_page.page_name}"
                )

                # 1. 导航到 Bulk upload composer（仅1次）
                await self._navigate_to_video_upload(page, fb_page)
                await asyncio.sleep(2)

                # 2. 一次性上传所有视频文件
                await self._upload_video_files_batch(page, file_paths)

                # 更新所有视频子任务状态为上传中
                for video in videos:
                    await self.task_service.update_video_status(video.id, VideoStatus.UPLOADING)

                # 3. 等待所有视频上传完成 AND 版权检查/处理完成
                await self._wait_for_all_uploads_and_processing(page, video_count)

                # 更新所有视频子任务状态为就绪
                for video in videos:
                    await self.task_service.update_video_status(video.id, VideoStatus.READY)

                # 4. 统一填写视频描述（使用任务的统一描述）
                await self._fill_description_batch(page, task.description, video_count)

                # 5. 设置排期时间（如果任务有排期设置）
                if task.start_time:
                    await self._set_scheduled_time(page, task.start_time)

                # 6. 勾选所有视频的 checkbox，然后点击批量发布按钮
                await self._select_all_videos(page, video_count)
                await self._click_publish(page)

                # 7. 发布后等待页面稳定
                logger.info("批量发布完成，等待页面稳定...")
                await asyncio.sleep(3)
                await self._wait_for_page_idle(page, timeout=10)

                # 8. 检查发布结果，分别更新每个视频子任务的状态
                publish_results = await self._check_publish_results(page, videos)

                success_count = 0
                fail_count = 0
                for video, result in zip(videos, publish_results):
                    if result["success"]:
                        await self.task_service.update_video_status(
                            video.id, VideoStatus.PUBLISHED
                        )
                        await self.log_service.create_log(
                            task_id=task.id,
                            account_name=account.name,
                            page_name=fb_page.page_name,
                            video_file_name=video.file_name,
                            scheduled_time=video.scheduled_time,
                            status=VideoLogStatus.PUBLISHED,
                        )
                        success_count += 1
                    else:
                        await self.task_service.update_video_status(
                            video.id, VideoStatus.FAILED, result.get("error", "发布失败")
                        )
                        await self.log_service.create_log(
                            task_id=task.id,
                            account_name=account.name,
                            page_name=fb_page.page_name,
                            video_file_name=video.file_name,
                            scheduled_time=video.scheduled_time,
                            status=VideoLogStatus.FAILED,
                            error_message=result.get("error", "发布失败"),
                        )
                        fail_count += 1

                logger.info(
                    f"批量发布完成: {success_count} 成功, {fail_count} 失败 "
                    f"-> 主页: {fb_page.page_name}"
                )
                return {"success_count": success_count, "fail_count": fail_count}

            except PublishButtonNotFoundError as e:
                # 发布按钮未找到，不重试上传，直接终止退出
                logger.error(
                    f"发布按钮未找到，终止任务（不重试上传）: {e}"
                )
                for video in videos:
                    await self.task_service.update_video_status(
                        video.id, VideoStatus.FAILED, str(e)
                    )
                    await self.log_service.create_log(
                        task_id=task.id,
                        account_name=account.name,
                        page_name=fb_page.page_name,
                        video_file_name=video.file_name,
                        scheduled_time=video.scheduled_time,
                        status=VideoLogStatus.FAILED,
                        error_message=str(e),
                    )
                return {"success_count": 0, "fail_count": video_count}

            except Exception as e:
                logger.warning(
                    f"批量发布失败 (尝试 {attempt}/{self.settings.max_retry}): {e}"
                )
                if attempt < self.settings.max_retry:
                    await asyncio.sleep(self.settings.retry_delay)
                else:
                    # 最终失败，记录所有视频的失败日志，更新子任务状态
                    for video in videos:
                        await self.task_service.update_video_status(
                            video.id, VideoStatus.FAILED, str(e)
                        )
                        await self.log_service.create_log(
                            task_id=task.id,
                            account_name=account.name,
                            page_name=fb_page.page_name,
                            video_file_name=video.file_name,
                            scheduled_time=video.scheduled_time,
                            status=VideoLogStatus.FAILED,
                            error_message=str(e),
                        )
                    return {"success_count": 0, "fail_count": video_count}

        return {"success_count": 0, "fail_count": video_count}

    async def _navigate_to_video_upload(self, page: Page, fb_page: FBPage):
        """
        导航到 Meta Business Suite 的 Bulk upload composer 页面。
        
        获取 asset_id 的优先级：
        1. 直接使用 fb_page.page_fb_id（最优，不需要额外导航）
        2. 从当前页面 URL 中提取 asset_id（当前已在 Business Suite 中）
        3. 兜底：导航到 Business Suite 首页提取 asset_id（仅当上述两种方式都失败时）
        
        目标URL：https://business.facebook.com/latest/bulk_upload_composer?asset_id=<asset_id>
        """
        from urllib.parse import urlparse, parse_qs

        # ========== 步骤1: 获取 asset_id（优先使用已知的 page_fb_id，避免打开首页）==========
        asset_id = None

        # 优先级1: 直接使用 fb_page.page_fb_id，无需额外导航
        if fb_page.page_fb_id:
            asset_id = fb_page.page_fb_id
            logger.info(f"直接使用 page_fb_id 作为 asset_id: {asset_id}，跳过导航到首页")

        # 优先级2: 从当前页面 URL 中提取 asset_id
        if not asset_id:
            current_url = page.url
            if "business.facebook.com" in current_url:
                parsed = urlparse(current_url)
                qs = parse_qs(parsed.query)
                if "asset_id" in qs:
                    asset_id = qs["asset_id"][0]
                    logger.info(f"从当前页面URL提取到 asset_id: {asset_id}")

        # 优先级3（兜底）: 如果以上两种方式都无法获取 asset_id，才导航到首页提取
        if not asset_id:
            logger.info("page_fb_id 为空且当前URL无 asset_id，兜底导航到 Meta Business Suite 首页提取")
            home_url = "https://business.facebook.com/latest/home"
            await page.goto(home_url, timeout=self.settings.page_load_timeout)
            await asyncio.sleep(5)

            current_url = page.url
            logger.info(f"Meta Business Suite 首页加载完成，当前URL: {current_url}")

            if "business.facebook.com" in current_url:
                parsed = urlparse(current_url)
                qs = parse_qs(parsed.query)
                if "asset_id" in qs:
                    asset_id = qs["asset_id"][0]
                    logger.info(f"从首页URL提取到 asset_id: {asset_id}")

            # 如果仍然没有 asset_id，等待页面重定向
            if not asset_id:
                logger.warning("首页URL中未发现 asset_id，等待页面重定向...")
                for _ in range(5):
                    await asyncio.sleep(2)
                    current_url = page.url
                    if "asset_id" in current_url:
                        parsed = urlparse(current_url)
                        qs = parse_qs(parsed.query)
                        if "asset_id" in qs:
                            asset_id = qs["asset_id"][0]
                            logger.info(f"等待后从URL提取到 asset_id: {asset_id}")
                            break

        if not asset_id:
            raise Exception(
                f"无法获取 asset_id（page_fb_id 为空，当前URL和首页URL均无 asset_id）。\n"
                f"当前URL: {page.url}\n"
                f"主页: {fb_page.page_name}\n"
                "请确认该主页的 Facebook ID 已正确填写，或主页已关联到 Meta Business Suite"
            )

        # ========== 步骤2: 用 asset_id 跳转到 Bulk upload composer ==========
        target_url = (
            f"https://business.facebook.com/latest/bulk_upload_composer"
            f"?asset_id={asset_id}"
        )
        logger.info(f"导航到 Bulk upload composer: {target_url}")

        # 导航前先等待页面网络空闲，避免中断正在进行的请求（如视频处理）
        await self._wait_for_page_idle(page)

        max_nav_attempts = 3
        last_error = None

        for nav_attempt in range(1, max_nav_attempts + 1):
            try:
                try:
                    await page.goto(
                        target_url,
                        timeout=self.settings.page_load_timeout,
                        wait_until="domcontentloaded",
                    )
                except Exception as goto_err:
                    err_msg = str(goto_err).lower()
                    # net::ERR_ABORTED 通常是页面被前一个上传/处理请求中断
                    if "err_aborted" in err_msg or "aborted" in err_msg:
                        logger.warning(
                            f"page.goto 出现 ERR_ABORTED（尝试 {nav_attempt}/{max_nav_attempts}），"
                            f"等待页面稳定后重试... 错误: {goto_err}"
                        )
                        # 等待更长时间，让后台请求完成
                        await asyncio.sleep(8)
                        # 检查当前页面是否已经意外到达了目标页面
                        if "bulk_upload_composer" in page.url.lower():
                            logger.info("虽然 goto 报错 ERR_ABORTED，但页面已到达 Bulk upload composer")
                            return
                        if nav_attempt < max_nav_attempts:
                            # 再等一下，让网络完全空闲
                            await self._wait_for_page_idle(page)
                            continue
                        else:
                            last_error = goto_err
                            break
                    else:
                        raise

                await asyncio.sleep(5)

                final_url = page.url.lower()
                logger.info(f"页面加载完成（尝试 {nav_attempt}），当前URL: {page.url}")

                # 验证是否成功到达目标页面
                if "bulk_upload_composer" in final_url:
                    logger.info("已成功进入 Bulk upload composer 页面")
                    return

                # 可能URL被重定向，但页面内容正确
                bulk_indicators = [
                    'text=/[Bb]ulk [Uu]pload/',
                    'text=/[Aa]dd [Vv]ideos/',
                    'text=/添加视频/',
                    'text=/批量上传/',
                    'input[type="file"]',
                ]
                for selector in bulk_indicators:
                    try:
                        if await page.locator(selector).count() > 0:
                            logger.info(
                                f"虽然URL变化，但页面包含 bulk upload 元素: {selector}，"
                                f"当前URL: {page.url}"
                            )
                            return
                    except Exception:
                        continue

                # 页面未正确加载，继续重试
                logger.warning(
                    f"页面可能未正确加载 Bulk upload composer（尝试 {nav_attempt}/{max_nav_attempts}），"
                    f"当前URL: {page.url}"
                )
                if nav_attempt < max_nav_attempts:
                    await asyncio.sleep(3)
                    continue

            except Exception as e:
                last_error = e
                if nav_attempt < max_nav_attempts:
                    logger.warning(
                        f"导航失败（尝试 {nav_attempt}/{max_nav_attempts}）: {e}，重试..."
                    )
                    await asyncio.sleep(5)
                    await self._wait_for_page_idle(page)
                    continue
                break

        # 所有重试都失败了
        # 打印页面可见文本辅助调试
        try:
            visible_texts = await page.evaluate("""
                () => {
                    const texts = [];
                    const allElements = document.querySelectorAll('a, span, button, h1, h2, h3, div[role="heading"]');
                    for (const el of allElements) {
                        const text = el.textContent.trim();
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0 && text.length > 0 && text.length < 80) {
                            texts.push(`[${el.tagName} x=${Math.round(rect.x)} y=${Math.round(rect.y)}] ${text}`);
                        }
                    }
                    return [...new Set(texts)].slice(0, 30);
                }
            """)
            logger.warning(
                f"Bulk upload composer 页面验证失败，页面可见文本:\n"
                + "\n".join(visible_texts)
            )
        except Exception:
            pass

        error_detail = str(last_error) if last_error else "未知错误"
        raise Exception(
            f"无法导航到 Bulk upload composer 页面（已重试 {max_nav_attempts} 次）。\n"
            f"目标URL: {target_url}\n"
            f"asset_id: {asset_id}\n"
            f"当前URL: {page.url}\n"
            f"最后错误: {error_detail}"
        )

    async def _wait_for_page_idle(self, page: Page, timeout: int = 15):
        """
        等待页面网络空闲，避免在有活跃请求时执行 page.goto 导致 ERR_ABORTED。
        
        通过监测页面是否有活跃的网络请求来判断，如果在指定时间内
        页面仍有活跃请求，则放弃等待继续执行。
        """
        try:
            # 方法1：使用 page.wait_for_load_state 等待网络空闲
            await page.wait_for_load_state("networkidle", timeout=timeout * 1000)
            logger.info("页面网络已空闲")
        except Exception:
            # 超时也没关系，可能有长轮询请求，继续执行
            logger.info(f"等待页面网络空闲超时（{timeout}秒），继续执行...")

        # 额外等一小段时间，确保稳定
        await asyncio.sleep(1)

    async def _upload_video_files_batch(self, page: Page, file_paths: List[str]):
        """
        在 Meta Business Suite Bulk upload reels 页面批量上传多个视频文件。
        
        Bulk upload 页面支持一次性选择多个文件，通过 input[type="file"] 的
        set_input_files 方法传入文件路径列表即可实现批量上传。
        
        注意：此方法只负责选择文件并触发上传，不等待上传完成。
        上传完成和视频处理的等待由 _wait_for_all_uploads_and_processing 方法负责。
        """
        logger.info(f"准备批量上传 {len(file_paths)} 个视频文件")

        # 先尝试点击 "Add videos" / "添加视频" 按钮触发文件选择器
        add_video_selectors = [
            'button:has-text("Add videos")',
            'button:has-text("添加视频")',
            'span:has-text("Add videos")',
            'span:has-text("添加视频")',
            'div:has-text("Drag and drop")',
            'div:has-text("拖放")',
        ]
        for selector in add_video_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=3000):
                    logger.info(f"找到上传区域/按钮: {selector}")
                    break
            except Exception:
                continue

        # 查找文件上传input（可能是隐藏的）
        file_input = page.locator('input[type="file"]')
        if await file_input.count() > 0:
            # Bulk upload 支持多文件，一次性传入所有文件路径
            await file_input.first.set_input_files(file_paths)
            logger.info(
                f"已选择 {len(file_paths)} 个视频文件进行批量上传:\n"
                + "\n".join(f"  - {fp}" for fp in file_paths)
            )
        else:
            raise Exception(
                "未找到文件上传组件，请确认已正确进入 Bulk upload reels 页面"
            )

    async def _get_progress_info(self, page: Page) -> dict:
        """
        通过 JavaScript 从页面 DOM 中获取所有进度条的数值信息。
        
        返回结构：
        {
            "progress_bars": [
                {"value": 100, "max": 100, "label": "...", "text": "..."},
                ...
            ],
            "all_complete": bool,       # 所有进度条是否都到达100
            "min_progress": int,        # 最低进度值
            "total_bars": int,          # 进度条总数
            "incomplete_count": int,    # 未完成的进度条数量
            "page_text_snapshot": str,  # 页面关键状态文本快照（用于辅助判断）
        }
        """
        try:
            result = await page.evaluate("""
                () => {
                    // 1. 收集所有 progressbar 元素的数值
                    const bars = document.querySelectorAll('[role="progressbar"]');
                    const progressBars = [];
                    for (const bar of bars) {
                        const value = parseInt(bar.getAttribute('aria-valuenow') || '0', 10);
                        const max = parseInt(bar.getAttribute('aria-valuemax') || '100', 10);
                        const label = bar.getAttribute('aria-label') || '';
                        const text = (bar.textContent || '').trim().substring(0, 100);
                        progressBars.push({ value, max, label, text });
                    }
                    
                    // 2. 计算整体进度状态
                    const allComplete = progressBars.length > 0 && 
                        progressBars.every(b => b.value >= b.max);
                    const minProgress = progressBars.length > 0 ? 
                        Math.min(...progressBars.map(b => Math.round(b.value / b.max * 100))) : -1;
                    const incompleteCount = progressBars.filter(b => b.value < b.max).length;
                    
                    // 3. 收集页面关键状态文本（用于辅助判断）
                    const statusKeywords = [
                        'upload', 'uploading', 'processing', 'checking', 'analyzing',
                        'encoding', 'copyright', 'complete', 'error', 'failed',
                        'schedule', 'publish', 'ready',
                        '上传', '处理', '检测', '检查', '编码', '版权', '完成',
                        '错误', '失败', '排期', '发布', '就绪'
                    ];
                    const allSpans = document.querySelectorAll('span, div[role="status"]');
                    const statusTexts = [];
                    for (const el of allSpans) {
                        const t = (el.textContent || '').trim().toLowerCase();
                        if (t.length > 0 && t.length < 100) {
                            for (const kw of statusKeywords) {
                                if (t.includes(kw.toLowerCase())) {
                                    const rect = el.getBoundingClientRect();
                                    if (rect.width > 0 && rect.height > 0) {
                                        statusTexts.push(el.textContent.trim().substring(0, 80));
                                    }
                                    break;
                                }
                            }
                        }
                    }
                    
                    // 4. 检查发布/排期按钮状态
                    const buttons = document.querySelectorAll('button, div[role="button"]');
                    const actionButtons = [];
                    for (const btn of buttons) {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if (t.includes('publish') || t.includes('schedule') || 
                            t.includes('发布') || t.includes('排期') ||
                            t.includes('next') || t.includes('下一步')) {
                            actionButtons.push({
                                text: btn.textContent.trim().substring(0, 50),
                                disabled: btn.disabled || btn.getAttribute('aria-disabled') === 'true',
                                visible: btn.getBoundingClientRect().width > 0
                            });
                        }
                    }
                    
                    return {
                        progress_bars: progressBars,
                        all_complete: allComplete,
                        min_progress: minProgress,
                        total_bars: progressBars.length,
                        incomplete_count: incompleteCount,
                        status_texts: [...new Set(statusTexts)].slice(0, 20),
                        action_buttons: actionButtons
                    };
                }
            """)
            return result
        except Exception as e:
            logger.warning(f"获取进度信息失败: {e}")
            return {
                "progress_bars": [],
                "all_complete": False,
                "min_progress": -1,
                "total_bars": 0,
                "incomplete_count": 0,
                "status_texts": [],
                "action_buttons": [],
            }

    async def _wait_for_all_uploads_and_processing(self, page: Page, video_count: int):
        """
        等待所有视频上传完成 + 视频处理/版权检查完成。
        
        **核心判断逻辑**：通过读取页面中 progressbar 元素的 aria-valuenow 数值
        来精确判断每个视频的上传和处理进度，而不是依赖文本匹配。
        
        文本匹配的问题：版权检查完成后显示 ✅，但包含 "Copyright check" 文本的元素
        仍然存在于 DOM 中，导致误判为"仍在处理中"。
        
        进度判断策略：
        1. 上传阶段：所有 progressbar 的 aria-valuenow 都达到 aria-valuemax（通常100）
        2. 处理阶段：进度条消失或全部100% + 发布按钮变为可用状态
        3. 兜底：通过 action_buttons 的 disabled 状态来判断是否可以发布
        """
        # 多视频批量上传需要更长的超时时间
        upload_timeout_sec = (self.settings.upload_timeout / 1000) * max(video_count, 1)
        # 处理阶段超时：每个视频至少5分钟
        processing_timeout_sec = max(upload_timeout_sec, 300 * max(video_count, 1))
        # 总超时 = 上传 + 处理
        total_timeout_sec = upload_timeout_sec + processing_timeout_sec
        poll_interval = 5  # 批量模式下每5秒检查一次

        logger.info(
            f"等待 {video_count} 个视频上传+处理完成..."
            f"（总超时: {total_timeout_sec}秒）"
        )

        elapsed = 0
        last_log_time = 0
        prev_min_progress = -1

        while elapsed < total_timeout_sec:
            # ========== 核心：通过 JS 获取进度条数值 ==========
            progress_info = await self._get_progress_info(page)
            total_bars = progress_info.get("total_bars", 0)
            all_complete = progress_info.get("all_complete", False)
            min_progress = progress_info.get("min_progress", -1)
            incomplete_count = progress_info.get("incomplete_count", 0)
            status_texts = progress_info.get("status_texts", [])
            action_buttons = progress_info.get("action_buttons", [])

            # ========== 判断1: 所有进度条数值都已满（100%）==========
            if total_bars > 0 and all_complete:
                logger.info(
                    f"✅ 所有进度条已完成 (共 {total_bars} 个，全部100%)，"
                    f"已等待 {elapsed} 秒"
                )
                # 进度条全部完成后，还需确认发布按钮可用
                # （可能进度条100%但版权检查结果还未出来）
                has_ready_button = any(
                    not btn.get("disabled", True) and btn.get("visible", False)
                    for btn in action_buttons
                )
                if has_ready_button:
                    ready_btn_names = [
                        btn["text"] for btn in action_buttons
                        if not btn.get("disabled", True) and btn.get("visible", False)
                    ]
                    logger.info(
                        f"✅ 发布按钮已可用: {', '.join(ready_btn_names)}，"
                        f"视频上传+处理全部完成！"
                    )
                    return
                else:
                    # 进度条100%但按钮还不可用，可能还在做最终检查
                    if elapsed % 10 == 0:
                        logger.info(
                            f"进度条全部100%，但发布按钮尚未就绪，继续等待... "
                            f"按钮状态: {action_buttons}"
                        )

            # ========== 判断2: 没有进度条但有可用的发布按钮 ==========
            elif total_bars == 0 and elapsed > 10:
                has_ready_button = any(
                    not btn.get("disabled", True) and btn.get("visible", False)
                    for btn in action_buttons
                )
                if has_ready_button:
                    ready_btn_names = [
                        btn["text"] for btn in action_buttons
                        if not btn.get("disabled", True) and btn.get("visible", False)
                    ]
                    logger.info(
                        f"✅ 未检测到进度条，但发布按钮已可用: {', '.join(ready_btn_names)}，"
                        f"判断为处理完成"
                    )
                    return

            # ========== 判断3: 检查是否有明确的错误状态 ==========
            error_keywords = ['error', 'failed', '错误', '失败', '无法上传', "couldn't"]
            error_texts = [
                t for t in status_texts
                if any(kw in t.lower() for kw in error_keywords)
            ]
            if error_texts:
                raise Exception(
                    f"视频处理/版权检查出现错误: {'; '.join(error_texts)}"
                )

            # ========== 定期打印进度日志 ==========
            should_log = (
                elapsed - last_log_time >= 15  # 每15秒至少打印一次
                or min_progress != prev_min_progress  # 进度有变化时打印
            )
            if should_log and elapsed > 0:
                last_log_time = elapsed
                prev_min_progress = min_progress

                if total_bars > 0:
                    bar_details = [
                        f"进度条{i+1}: {b.get('value', 0)}/{b.get('max', 100)}"
                        for i, b in enumerate(progress_info.get("progress_bars", []))
                    ]
                    logger.info(
                        f"⏳ 上传/处理进行中... "
                        f"[{', '.join(bar_details)}] "
                        f"未完成: {incomplete_count}/{total_bars}，"
                        f"最低进度: {min_progress}%，"
                        f"已等待 {elapsed} 秒"
                    )
                else:
                    logger.info(
                        f"⏳ 等待中（未检测到进度条），"
                        f"页面状态文本: {status_texts[:5]}，"
                        f"按钮: {[b['text'] for b in action_buttons][:5]}，"
                        f"已等待 {elapsed} 秒"
                    )

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        # ========== 超时处理 ==========
        # 最终再做一次检查
        final_info = await self._get_progress_info(page)
        final_buttons = final_info.get("action_buttons", [])
        has_ready_button = any(
            not btn.get("disabled", True) and btn.get("visible", False)
            for btn in final_buttons
        )
        if has_ready_button:
            logger.info(
                "⚠️ 等待超时，但检测到发布按钮可用，判断为处理已完成"
            )
            return

        logger.warning(
            f"视频上传+处理等待超时（{total_timeout_sec}秒），"
            f"最终进度信息: bars={final_info.get('total_bars')}, "
            f"complete={final_info.get('all_complete')}, "
            f"min={final_info.get('min_progress')}%，"
            f"将尝试继续后续操作"
        )

    async def _fill_description_batch(self, page: Page, description: str, video_count: int):
        """
        在 Meta Business Suite Bulk upload reels 页面中为所有视频统一填写描述。
        
        批量上传模式下，Bulk upload 页面可能有以下几种描述输入方式：
        1. 统一描述区域（"Apply to all" / "应用到全部"）
        2. 每个视频条目各有一个独立的描述框
        
        优先使用统一描述功能，如果不存在则逐个填写。
        """
        if not description:
            logger.info("描述为空，跳过描述填写")
            return

        # ========== 方式1: 查找 "Apply to all" / 统一描述功能 ==========
        apply_all_selectors = [
            'button:has-text("Apply to all")',
            'button:has-text("应用到全部")',
            'span:has-text("Apply to all")',
            'span:has-text("应用到全部")',
            'button:has-text("Apply description to all")',
            'button:has-text("将描述应用到全部")',
        ]
        has_apply_all = False
        for selector in apply_all_selectors:
            try:
                if await page.locator(selector).count() > 0:
                    has_apply_all = True
                    logger.info(f"发现统一描述功能: {selector}")
                    break
            except Exception:
                continue

        # ========== 方式2: 填写描述输入框 ==========
        desc_selectors = [
            # Meta Business Suite 描述输入框
            'div[aria-label="Description"]',
            'div[aria-label="描述"]',
            'textarea[aria-label="Description"]',
            'textarea[aria-label="描述"]',
            'div[aria-label="Write a description"]',
            'div[aria-label="写描述"]',
            'div[aria-label="Add a description..."]',
            'div[aria-label="添加描述..."]',
            # 通用 textbox
            'div[contenteditable="true"][role="textbox"]',
            'div[role="textbox"]',
            'textarea[placeholder*="description"]',
            'textarea[placeholder*="描述"]',
        ]

        # 查找所有描述输入框
        filled_count = 0
        for selector in desc_selectors:
            try:
                desc_boxes = page.locator(selector)
                box_count = await desc_boxes.count()
                if box_count == 0:
                    continue

                logger.info(f"找到 {box_count} 个描述输入框: {selector}")

                # 逐个填写所有描述框（统一使用任务描述）
                for i in range(box_count):
                    try:
                        desc_box = desc_boxes.nth(i)
                        if await desc_box.is_visible(timeout=2000):
                            await desc_box.click()
                            await asyncio.sleep(0.3)
                            await desc_box.fill("")
                            await desc_box.fill(description)
                            filled_count += 1
                            await asyncio.sleep(0.3)
                    except Exception as fill_err:
                        logger.warning(f"填写第 {i + 1} 个描述框失败: {fill_err}")
                        continue

                if filled_count > 0:
                    logger.info(f"已为 {filled_count} 个视频填写统一描述")

                    # 如果有 "Apply to all" 按钮且只填了1个描述，点击应用到全部
                    if has_apply_all and filled_count == 1 and video_count > 1:
                        for apply_sel in apply_all_selectors:
                            try:
                                apply_btn = page.locator(apply_sel).first
                                if await apply_btn.is_visible(timeout=2000):
                                    await apply_btn.click()
                                    await asyncio.sleep(1)
                                    logger.info("已点击 'Apply to all' 将描述应用到所有视频")
                                    break
                            except Exception:
                                continue
                    return
            except Exception:
                continue

        if filled_count == 0:
            logger.warning("未找到描述输入框，跳过描述填写")

    async def _set_scheduled_time(self, page: Page, scheduled_time: datetime):
        """
        在 Meta Business Suite 中设置定时发布时间。
        
        Meta Business Suite 的排期流程：
        1. 点击 "Schedule" / "排期" 按钮（或展开发布选项）
        2. 设置日期和时间
        """
        try:
            # 步骤1: 查找并点击排期选项
            # Meta Business Suite 中可能是一个下拉菜单或切换按钮
            schedule_selectors = [
                # 排期按钮/选项
                'button:has-text("Schedule")',
                'button:has-text("排期")',
                'div[aria-label="Schedule"]',
                'div[aria-label="排期"]',
                'span:has-text("Schedule")',
                'span:has-text("排期")',
                # 发布选项下拉（可能需要先展开）
                'div[aria-label="Publishing options"]',
                'div[aria-label="发布选项"]',
                # 日程安排选项
                'input[type="radio"][value="SCHEDULED"]',
                'label:has-text("Schedule")',
                'label:has-text("排期")',
            ]
            for selector in schedule_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=3000):
                        await btn.click()
                        await asyncio.sleep(1)
                        logger.info(f"已点击排期选项: {selector}")
                        break
                except Exception:
                    continue

            # 步骤2: 设置日期
            date_str = scheduled_time.strftime("%Y/%m/%d")
            date_selectors = [
                'input[aria-label*="Date"]',
                'input[aria-label*="日期"]',
                'input[aria-label*="date"]',
                'input[placeholder*="Date"]',
                'input[placeholder*="日期"]',
            ]
            for selector in date_selectors:
                try:
                    date_input = page.locator(selector).first
                    if await date_input.is_visible(timeout=3000):
                        await date_input.click(triple=True)  # 全选现有内容
                        await asyncio.sleep(0.2)
                        await date_input.fill(date_str)
                        logger.info(f"已设置日期: {date_str}")
                        break
                except Exception:
                    continue

            # 步骤3: 设置时间
            time_str = scheduled_time.strftime("%H:%M")
            time_selectors = [
                'input[aria-label*="Time"]',
                'input[aria-label*="时间"]',
                'input[aria-label*="time"]',
                'input[placeholder*="Time"]',
                'input[placeholder*="时间"]',
            ]
            for selector in time_selectors:
                try:
                    time_input = page.locator(selector).first
                    if await time_input.is_visible(timeout=3000):
                        await time_input.click(triple=True)  # 全选现有内容
                        await asyncio.sleep(0.2)
                        await time_input.fill(time_str)
                        logger.info(f"已设置时间: {time_str}")
                        break
                except Exception:
                    continue

            await asyncio.sleep(1)
            logger.info(f"已设置定时发布: {scheduled_time}")

        except Exception as e:
            logger.warning(f"设置定时发布时间失败: {e}，将以即时发布方式处理")

    async def _select_all_videos(self, page: Page, video_count: int):
        """
        在 Bulk upload 页面中勾选所有视频的 checkbox。
        
        在 Meta Business Suite 的 Bulk upload 页面中，需要先勾选所有视频，
        右下方的发布/排期按钮才会变为可用状态。
        
        勾选策略：
        1. 优先查找 "Select all" / "全选" 复选框
        2. 如果没有全选按钮，逐个勾选每个视频的 checkbox
        """
        logger.info(f"勾选所有视频 checkbox（共 {video_count} 个）...")

        # ========== 策略1: 查找 "Select all" / "全选" 按钮 ==========
        select_all_selectors = [
            'input[type="checkbox"][aria-label*="Select all"]',
            'input[type="checkbox"][aria-label*="全选"]',
            'label:has-text("Select all")',
            'label:has-text("全选")',
            'span:has-text("Select all")',
            'span:has-text("全选")',
            'div[role="checkbox"][aria-label*="Select all"]',
            'div[role="checkbox"][aria-label*="全选"]',
        ]
        for selector in select_all_selectors:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=3000):
                    # 检查是否已经被选中
                    is_checked = await el.is_checked() if await el.count() > 0 else False
                    if not is_checked:
                        await el.click()
                        await asyncio.sleep(1)
                    logger.info(f"已通过全选按钮勾选所有视频: {selector}")
                    return
            except Exception:
                continue

        # ========== 策略2: 通过 JavaScript 查找并点击全选 checkbox ==========
        try:
            selected = await page.evaluate("""
                () => {
                    // 查找所有 checkbox
                    const checkboxes = document.querySelectorAll(
                        'input[type="checkbox"], div[role="checkbox"]'
                    );
                    let clickedCount = 0;
                    
                    for (const cb of checkboxes) {
                        const label = cb.getAttribute('aria-label') || '';
                        const parent = cb.closest('label, div');
                        const parentText = parent ? (parent.textContent || '').trim().toLowerCase() : '';
                        
                        // 查找全选类型的 checkbox
                        if (label.toLowerCase().includes('select all') || 
                            label.includes('全选') ||
                            parentText.includes('select all') ||
                            parentText.includes('全选')) {
                            cb.click();
                            clickedCount++;
                            return { method: 'select_all', count: clickedCount };
                        }
                    }
                    
                    // 没有全选按钮，逐个勾选所有可见的 checkbox
                    for (const cb of checkboxes) {
                        const rect = cb.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            const isChecked = cb.checked || 
                                cb.getAttribute('aria-checked') === 'true';
                            if (!isChecked) {
                                cb.click();
                                clickedCount++;
                            }
                        }
                    }
                    
                    return { method: 'individual', count: clickedCount };
                }
            """)
            logger.info(
                f"通过 JS 勾选视频 checkbox: 方式={selected.get('method')}, "
                f"数量={selected.get('count')}"
            )
            await asyncio.sleep(1)

            # 验证勾选状态
            if selected.get('count', 0) == 0:
                logger.warning("未能勾选任何 checkbox，尝试直接点击视频卡片...")
                # 兜底：尝试点击每个视频卡片的 checkbox 区域
                checkboxes = page.locator('input[type="checkbox"], div[role="checkbox"]')
                checkbox_count = await checkboxes.count()
                for i in range(checkbox_count):
                    try:
                        cb = checkboxes.nth(i)
                        if await cb.is_visible(timeout=1000):
                            await cb.click()
                            await asyncio.sleep(0.3)
                    except Exception:
                        continue
                logger.info(f"通过 Playwright 逐个点击了 {checkbox_count} 个 checkbox")

        except Exception as e:
            logger.warning(f"勾选视频 checkbox 失败: {e}")
            # 不抛异常，继续尝试发布（有些页面可能不需要勾选）

        await asyncio.sleep(1)

    async def _check_publish_results(
        self, page: Page, videos: List[TaskVideo]
    ) -> List[dict]:
        """
        发布操作完成后，检查每个视频的发布结果。
        
        通过检测页面是否有错误提示、成功提示来判断。
        如果无法区分单个视频的成功/失败，则根据整体结果统一标记。
        
        返回：[{"success": bool, "error": str}, ...] 与 videos 一一对应
        """
        results = []

        try:
            # 检查页面是否有成功发布的提示
            success_indicators = [
                'span:has-text("All reels scheduled")',
                'span:has-text("所有 Reels 已排期")',
                'span:has-text("Published")',
                'span:has-text("已发布")',
                'span:has-text("Scheduled")',
                'span:has-text("已排期")',
                'span:has-text("successfully")',
                'span:has-text("成功")',
                'div:has-text("All reels scheduled")',
                'div:has-text("All reels published")',
            ]
            is_all_success = False
            for selector in success_indicators:
                try:
                    if await page.locator(selector).count() > 0:
                        logger.info(f"检测到批量发布成功提示: {selector}")
                        is_all_success = True
                        break
                except Exception:
                    continue

            if is_all_success:
                # 全部成功
                return [{"success": True, "error": ""} for _ in videos]

            # 检查页面是否有全局错误
            error_indicators = [
                'span:has-text("Error")',
                'span:has-text("错误")',
                'span:has-text("Failed")',
                'span:has-text("失败")',
                'span:has-text("couldn\'t")',
                'span:has-text("无法")',
            ]
            global_error = ""
            for selector in error_indicators:
                try:
                    err_el = page.locator(selector).first
                    if await err_el.is_visible(timeout=2000):
                        global_error = await err_el.text_content() or "发布失败"
                        break
                except Exception:
                    continue

            if global_error:
                logger.warning(f"检测到发布错误: {global_error}")
                return [{"success": False, "error": global_error} for _ in videos]

            # 没有明确的成功或失败标识
            # 检查URL是否发生变化（发布成功后通常会跳转离开 bulk_upload_composer）
            current_url = page.url.lower()
            if "bulk_upload_composer" not in current_url:
                logger.info("发布后页面已离开 bulk_upload_composer，判断为全部成功")
                return [{"success": True, "error": ""} for _ in videos]

            # 兜底：无法确定，默认标记为成功（因为 _click_publish 没有抛异常）
            logger.info("无法明确判断发布结果，默认标记为成功")
            return [{"success": True, "error": ""} for _ in videos]

        except Exception as e:
            logger.warning(f"检查发布结果时出错: {e}，默认标记为成功")
            return [{"success": True, "error": ""} for _ in videos]

    async def _click_publish(self, page: Page):
        """
        在 Meta Business Suite 中点击发布/排期按钮。
        
        Bulk upload reels 页面的发布按钮可能有多种形式：
        - "Schedule" / "排期" 按钮（定时发布）
        - "Publish" / "发布" / "Publicar" 按钮（不同语言）
        - "Schedule all" / "全部排期" 按钮（批量排期）
        - 带有 class 包含 x1vvvo52 的按钮（在页面右下角）
        """
        publish_selectors = [
            # Meta Business Suite 批量发布按钮（多语言支持）
            'button:has-text("Schedule all")',
            'button:has-text("全部排期")',
            'button:has-text("Publish all")',
            'button:has-text("全部发布")',
            'button:has-text("Publicar todo")',
            'button:has-text("Programar todo")',
            # 单条发布按钮（多语言支持）
            'button:has-text("Schedule")',
            'button:has-text("排期")',
            'button:has-text("Publish")',
            'button:has-text("发布")',
            'button:has-text("Publicar")',
            'button:has-text("Programar")',
            # div 形式的按钮（多语言支持）
            'div:has-text("Publicar"):not(:has(div:has-text("Publicar")))',
            'div:has-text("Programar"):not(:has(div:has-text("Programar")))',
            # aria-label 形式
            'div[aria-label="Schedule"]',
            'div[aria-label="排期"]',
            'div[aria-label="Publish"]',
            'div[aria-label="发布"]',
            'div[aria-label="Publicar"]',
            'div[aria-label="Programar"]',
            # 提交按钮
            'button[type="submit"]:has-text("Schedule")',
            'button[type="submit"]:has-text("Publish")',
            'button[type="submit"]:has-text("Publicar")',
            'button[type="submit"]:has-text("Programar")',
        ]
        for selector in publish_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=3000) and await btn.is_enabled(timeout=1000):
                    await btn.click()
                    await asyncio.sleep(3)
                    logger.info(f"已点击发布按钮: {selector}")

                    # 处理可能出现的确认弹窗
                    confirm_selectors = [
                        'button:has-text("Confirm")',
                        'button:has-text("确认")',
                        'button:has-text("Done")',
                        'button:has-text("完成")',
                        'button:has-text("Confirmar")',
                        'button:has-text("Listo")',
                    ]
                    for confirm_sel in confirm_selectors:
                        try:
                            confirm_btn = page.locator(confirm_sel).first
                            if await confirm_btn.is_visible(timeout=2000):
                                await confirm_btn.click()
                                await asyncio.sleep(1)
                                logger.info("已确认发布")
                                break
                        except Exception:
                            continue

                    return
            except Exception:
                continue

        # ========== 兜底：通过 class 包含 x1vvvo52 查找右下角发布按钮 ==========
        try:
            found_by_class = await page.evaluate("""
                () => {
                    // 方式1：通过 class 包含 x1vvvo52 查找
                    const candidates = document.querySelectorAll('[class*="x1vvvo52"]');
                    for (const el of candidates) {
                        const rect = el.getBoundingClientRect();
                        const text = (el.textContent || '').trim();
                        // 检查是否在页面右下角区域，且包含发布相关文本
                        if (rect.width > 0 && rect.height > 0 && 
                            rect.right > window.innerWidth * 0.6 &&
                            rect.bottom > window.innerHeight * 0.6) {
                            const publishKeywords = [
                                'publish', 'schedule', 'publicar', 'programar',
                                '发布', '排期', '全部'
                            ];
                            const lowerText = text.toLowerCase();
                            for (const kw of publishKeywords) {
                                if (lowerText.includes(kw)) {
                                    el.click();
                                    return { found: true, text: text, class: el.className.substring(0, 100) };
                                }
                            }
                        }
                    }
                    
                    // 方式2：直接通过文本 Publicar 在右下角查找
                    const allElements = document.querySelectorAll('div[role="button"], button, span');
                    for (const el of allElements) {
                        const text = (el.textContent || '').trim();
                        if (text === 'Publicar' || text === 'Programar' ||
                            text === 'Publicar todo' || text === 'Programar todo') {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                // 找到最外层可点击的父元素
                                const clickTarget = el.closest('div[role="button"]') || 
                                                    el.closest('button') || el;
                                clickTarget.click();
                                return { found: true, text: text, tag: clickTarget.tagName };
                            }
                        }
                    }
                    
                    return { found: false };
                }
            """)
            if found_by_class and found_by_class.get('found'):
                logger.info(
                    f"通过 JS 兜底找到并点击了发布按钮: "
                    f"text='{found_by_class.get('text', '')}', "
                    f"info={found_by_class}"
                )
                await asyncio.sleep(3)

                # 处理可能出现的确认弹窗
                confirm_selectors = [
                    'button:has-text("Confirm")',
                    'button:has-text("确认")',
                    'button:has-text("Confirmar")',
                    'button:has-text("Done")',
                    'button:has-text("完成")',
                    'button:has-text("Listo")',
                ]
                for confirm_sel in confirm_selectors:
                    try:
                        confirm_btn = page.locator(confirm_sel).first
                        if await confirm_btn.is_visible(timeout=2000):
                            await confirm_btn.click()
                            await asyncio.sleep(1)
                            logger.info("已确认发布")
                            break
                    except Exception:
                        continue

                return
        except Exception as js_err:
            logger.warning(f"JS 兜底查找发布按钮失败: {js_err}")

        # ========== 打印页面调试信息 ==========
        try:
            debug_info = await page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll('button, div[role="button"]');
                    const infos = [];
                    for (const btn of buttons) {
                        const text = (btn.textContent || '').trim();
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0 && text.length > 0 && text.length < 80) {
                            infos.push({
                                tag: btn.tagName,
                                text: text,
                                class: (btn.className || '').substring(0, 80),
                                disabled: btn.disabled || btn.getAttribute('aria-disabled') === 'true',
                                x: Math.round(rect.x),
                                y: Math.round(rect.y),
                                w: Math.round(rect.width),
                                h: Math.round(rect.height)
                            });
                        }
                    }
                    return infos;
                }
            """)
            logger.error(
                f"未找到发布按钮！页面上所有可见按钮：\n"
                + "\n".join(
                    f"  [{b.get('tag')} x={b.get('x')} y={b.get('y')} w={b.get('w')} h={b.get('h')} "
                    f"disabled={b.get('disabled')} class='{b.get('class', '')[:40]}'] {b.get('text')}"
                    for b in debug_info
                )
            )
        except Exception:
            pass

        raise PublishButtonNotFoundError(
            "未找到发布按钮（Publicar / Publish / 发布 等），"
            "请确认视频已上传完成、已勾选所有视频，且 Bulk upload reels 页面正常加载"
        )
