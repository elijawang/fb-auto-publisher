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

    async def retry_failed_videos(
        self, task_id: str, page_name: str, video_ids: List[str]
    ) -> dict:
        """
        重试指定的失败视频子任务。
        
        流程：
        1. 启动浏览器并登录
        2. 切换到对应的公共主页
        3. 仅对指定的失败视频重新上传并发布
        4. 更新子任务状态
        5. 重新推断主任务状态
        """
        task = await self.task_service.get_task(task_id)
        if not task:
            return {"success": False, "message": "任务不存在"}

        # 获取要重试的视频子任务
        from sqlalchemy import select
        result = await self.session.execute(
            select(TaskVideo).where(TaskVideo.id.in_(video_ids))
        )
        retry_videos = list(result.scalars().all())
        if not retry_videos:
            return {"success": False, "message": "未找到要重试的视频子任务"}

        # 获取账号和主页信息
        from sqlalchemy.orm import selectinload
        acc_result = await self.session.execute(
            select(FBAccount)
            .options(selectinload(FBAccount.pages))
            .where(FBAccount.id == task.account_id)
        )
        account = acc_result.scalar_one_or_none()
        if not account:
            return {"success": False, "message": "账号不存在"}

        # 找到对应的公共主页
        fb_page = None
        for p in account.pages:
            if p.page_name == page_name:
                fb_page = p
                break
        if not fb_page:
            return {"success": False, "message": f"未找到公共主页: {page_name}"}

        logger.info(
            f"重试发布 {len(retry_videos)} 个失败视频 -> 主页: {page_name}"
        )

        try:
            # 步骤1: 启动浏览器并登录
            login_result = await self.browser_manager.login_facebook(
                account.id, wait_for_auth=True
            )
            if not login_result["success"]:
                for v in retry_videos:
                    await self.task_service.update_video_status(
                        v.id, VideoStatus.FAILED, f"登录失败: {login_result['message']}"
                    )
                await self.task_service.finalize_task_status(task_id)
                return {"success": False, "message": f"登录失败: {login_result['message']}"}

            page = await self.browser_manager.get_page(account.id)
            if not page:
                await self.task_service.finalize_task_status(task_id)
                return {"success": False, "message": "无法获取浏览器页面"}

            # 步骤2: 切换到对应主页并批量发布
            await self._switch_to_page(page, fb_page)
            await asyncio.sleep(random.uniform(1, 3))

            batch_result = await self._publish_videos_batch(
                page=page,
                task=task,
                videos=retry_videos,
                fb_page=fb_page,
                account=account,
            )

            # 步骤3: 关闭浏览器
            await self.browser_manager.close_browser(account.id)

            # 步骤4: 更新主任务状态
            final_status = await self.task_service.finalize_task_status(task_id)

            summary = (
                f"重试完成 | 成功: {batch_result['success_count']} | "
                f"失败: {batch_result['fail_count']} | 最终状态: {final_status.value}"
            )
            logger.info(summary)
            return {"success": batch_result['fail_count'] == 0, "message": summary}

        except Exception as e:
            logger.error(f"重试视频异常: {e}")
            for v in retry_videos:
                await self.task_service.update_video_status(
                    v.id, VideoStatus.FAILED, str(e)
                )
            await self.task_service.finalize_task_status(task_id)
            await self.browser_manager.close_browser(account.id)
            return {"success": False, "message": f"重试异常: {str(e)}"}

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

                # 5. 为每个视频设置定时发布时间（从任务起始时间开始，按间隔递增）
                await self._set_scheduled_times_per_video(page, videos)

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

    async def _set_scheduled_times_per_video(self, page: Page, videos: List[TaskVideo]):
        """
        在 Meta Business Suite Bulk upload 页面中，为每个视频逐个设置定时发布时间。
        
        每个视频的 scheduled_time 已在创建任务时根据起始时间和间隔自动计算。
        
        Meta Business Suite Bulk upload 页面的表格结构：
        - 每行视频有多列：checkbox、缩略图/标题/描述、标签、平台、**发布选项（第5列）**、...
        - 第5列默认显示 "Publicar ahora"（立即发布）/ "Publish now" / "立即发布"
        - 需要点击第5列展开发布选项弹窗，弹窗中含有多个tab选项：
          - 第1个tab: 立即发布（Publicar ahora / Publish now）
          - 第2个tab: 定时发布（Programar / Schedule）
          - 第3个tab: 其他选项
        - 选择第2个tab（Programar）后会出现日期和时间输入框，填入对应的 scheduled_time
        
        此方法的核心流程（对每个视频）：
        1. 找到当前视频所在行的第5列（发布选项列）
        2. 点击第5列展开发布选项弹窗
        3. 在弹出的tab面板中点击第2个tab（Programar / Schedule / 排期）
        4. 在出现的日期和时间输入框中填写 scheduled_time
        5. 关闭面板/确认
        """
        video_count = len(videos)
        logger.info(f"开始为 {video_count} 个视频逐个设置定时发布时间（第5列发布选项）...")

        try:
            for i, video in enumerate(videos):
                scheduled_time = video.scheduled_time
                if not scheduled_time:
                    logger.warning(f"视频 {video.file_name} 没有排期时间，跳过")
                    continue

                try:
                    # ========== 步骤1: 通过 JS 定位第 i 行的第5列发布选项并点击 ==========
                    clicked = await self._click_publish_option_cell(page, i)
                    
                    if not clicked:
                        logger.warning(
                            f"视频 {i + 1}/{video_count} ({video.file_name}) "
                            f"未能定位到第5列发布选项，尝试备选方案..."
                        )
                        # 备选方案：尝试通过文本定位
                        clicked = await self._click_publish_option_by_text(page, i)
                    
                    if not clicked:
                        logger.warning(
                            f"视频 {i + 1}/{video_count} ({video.file_name}) "
                            f"无法点击发布选项，跳过"
                        )
                        continue
                    
                    await asyncio.sleep(1)

                    # ========== 步骤2: 在下拉菜单中选择 "Programar" / "Schedule" ==========
                    await self._switch_to_schedule_mode(page)
                    await asyncio.sleep(1)

                    # ========== 步骤3: 填写日期和时间 ==========
                    await self._fill_schedule_datetime(page, scheduled_time)

                    # ========== 步骤4: 关闭排期面板 ==========
                    await self._close_schedule_panel(page)
                    await asyncio.sleep(0.5)

                    logger.info(
                        f"✅ 视频 {i + 1}/{video_count} ({video.file_name}) "
                        f"定时发布时间设置为: {scheduled_time}"
                    )
                except Exception as e:
                    logger.warning(
                        f"设置视频 {i + 1}/{video_count} ({video.file_name}) "
                        f"排期时间失败: {e}，继续下一个"
                    )
                    # 确保关闭可能残留的弹窗/面板
                    try:
                        await page.keyboard.press("Escape")
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass
                    continue

            logger.info(f"所有视频定时发布时间设置完成")

        except Exception as e:
            logger.warning(f"设置定时发布时间失败: {e}，将以默认方式处理")

    async def _click_publish_option_cell(self, page: Page, row_index: int) -> bool:
        """
        通过 JavaScript 精确定位 Bulk upload 表格中第 row_index 行的第5列（发布选项列），
        并点击该单元格展开发布选项弹窗。
        
        Meta Business Suite Bulk upload 页面的行结构：
        - 行容器可能是 div[role="row"], tr, 或具有特定 data-testid 的 div
        - 每行的子元素（列/单元格）按顺序排列
        - 第5列（index=4）通常包含 "Publicar ahora" / "Publish now" / "立即发布" 文本
        
        返回 True 表示成功点击，False 表示未找到目标。
        """
        result = await page.evaluate("""
            (rowIndex) => {
                // ========== 策略1: 通过 role="row" 查找表格行 ==========
                let rows = document.querySelectorAll('div[role="row"], tr[role="row"]');
                
                // 过滤掉表头行（可能是第一个 role="row"）
                const dataRows = [];
                for (const row of rows) {
                    // 如果行包含视频文件相关内容（如缩略图、视频名），认为是数据行
                    const hasMedia = row.querySelector('video, img, [role="img"]');
                    const hasInput = row.querySelector('input[type="checkbox"]');
                    if (hasMedia || hasInput) {
                        dataRows.push(row);
                    }
                }
                
                // 如果通过 role="row" 找到的数据行不够，尝试其他选择器
                let targetRows = dataRows.length > rowIndex ? dataRows : null;
                
                if (!targetRows) {
                    // ========== 策略2: 查找所有视频条目容器 ==========
                    // Bulk upload 页面中每个视频可能是一个独立的容器 div
                    const containers = document.querySelectorAll(
                        'div[data-testid*="video"], div[data-testid*="reel"], ' +
                        'div[data-testid*="upload"], div[role="listitem"]'
                    );
                    if (containers.length > rowIndex) {
                        targetRows = Array.from(containers);
                    }
                }
                
                if (!targetRows || targetRows.length <= rowIndex) {
                    // ========== 策略3: 在整个页面中按位置查找所有包含发布选项文本的元素 ==========
                    const publishOptionKeywords = [
                        'publicar ahora', 'publish now', '立即发布',
                        'programar', 'schedule', '排期', '定时发布',
                        'programada', 'scheduled', '已排期'
                    ];
                    const allClickable = document.querySelectorAll(
                        'div[role="button"], div[aria-haspopup], div[tabindex], span[role="button"]'
                    );
                    const publishOptionElements = [];
                    for (const el of allClickable) {
                        const text = (el.textContent || '').trim().toLowerCase();
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            for (const kw of publishOptionKeywords) {
                                if (text.includes(kw) && text.length < 80) {
                                    publishOptionElements.push({
                                        element: el,
                                        text: text,
                                        y: rect.y
                                    });
                                    break;
                                }
                            }
                        }
                    }
                    
                    // 按 Y 坐标排序（从上到下对应每个视频行）
                    publishOptionElements.sort((a, b) => a.y - b.y);
                    
                    // 去重（同一行可能有多个匹配元素，取Y坐标相近的第一个）
                    const uniqueByRow = [];
                    let lastY = -100;
                    for (const item of publishOptionElements) {
                        if (Math.abs(item.y - lastY) > 20) {
                            uniqueByRow.push(item);
                            lastY = item.y;
                        }
                    }
                    
                    if (uniqueByRow.length > rowIndex) {
                        uniqueByRow[rowIndex].element.click();
                        return {
                            success: true,
                            method: 'text_position',
                            text: uniqueByRow[rowIndex].text,
                            total: uniqueByRow.length
                        };
                    }
                    
                    return {
                        success: false,
                        method: 'none',
                        dataRowCount: dataRows.length,
                        publishOptionCount: publishOptionElements.length,
                        uniqueRowCount: uniqueByRow ? uniqueByRow.length : 0
                    };
                }
                
                // ========== 定位到目标行的第4列 ==========
                const targetRow = targetRows[rowIndex];
                
                // 获取行内所有直接子元素（列/单元格）
                const cells = targetRow.querySelectorAll(':scope > div, :scope > td');
                
                if (cells.length >= 5) {
                    // 直接点击第5列（index=4）
                    const cell5 = cells[4];
                    // 在第5列中查找可点击元素
                    const clickTarget = cell5.querySelector(
                        'div[role="button"], div[aria-haspopup], div[tabindex], ' +
                        'span[role="button"], button'
                    ) || cell5;
                    clickTarget.click();
                    return {
                        success: true,
                        method: 'cell_index',
                        cellCount: cells.length,
                        cellText: (cell5.textContent || '').trim().substring(0, 80)
                    };
                }
                
                // 如果直接子元素不到5个，在行内查找包含发布选项关键词的元素
                const publishKeywords = [
                    'publicar ahora', 'publish now', '立即发布',
                    'programar', 'schedule', '排期',
                    'programada', 'scheduled', '已排期'
                ];
                const rowClickables = targetRow.querySelectorAll(
                    'div[role="button"], div[aria-haspopup], div[tabindex], ' +
                    'span[role="button"], button, select'
                );
                for (const el of rowClickables) {
                    const text = (el.textContent || '').trim().toLowerCase();
                    for (const kw of publishKeywords) {
                        if (text.includes(kw)) {
                            el.click();
                            return {
                                success: true,
                                method: 'keyword_in_row',
                                text: text.substring(0, 80)
                            };
                        }
                    }
                }
                
                return {
                    success: false,
                    method: 'row_found_but_no_cell',
                    cellCount: cells.length,
                    rowText: (targetRow.textContent || '').trim().substring(0, 200)
                };
            }
        """, row_index)

        if result and result.get('success'):
            logger.info(
                f"已点击第 {row_index + 1} 行发布选项 "
                f"(方式: {result.get('method')}, 文本: {result.get('text', result.get('cellText', 'N/A'))})"
            )
            return True
        else:
            logger.warning(f"第 {row_index + 1} 行发布选项定位失败: {result}")
            return False

    async def _click_publish_option_by_text(self, page: Page, row_index: int) -> bool:
        """
        备选方案：通过 Playwright 选择器文本匹配的方式，
        找到页面上第 row_index 个 "Publicar ahora" / "Publish now" / "立即发布" 元素并点击。
        
        这种方式不依赖行结构，而是直接查找所有发布选项文本元素。
        """
        publish_now_selectors = [
            'span:has-text("Publicar ahora")',
            'span:has-text("Publish now")',
            'span:has-text("立即发布")',
            'div[role="button"]:has-text("Publicar ahora")',
            'div[role="button"]:has-text("Publish now")',
            'div[role="button"]:has-text("立即发布")',
            # 已经设为定时的也要能点击重新设置
            'span:has-text("Programada")',
            'span:has-text("Scheduled")',
            'span:has-text("已排期")',
            'div[role="button"]:has-text("Programada")',
            'div[role="button"]:has-text("Scheduled")',
            'div[role="button"]:has-text("已排期")',
        ]
        
        for selector in publish_now_selectors:
            try:
                elements = page.locator(selector)
                count = await elements.count()
                if count > row_index:
                    el = elements.nth(row_index)
                    if await el.is_visible(timeout=2000):
                        await el.click()
                        logger.info(
                            f"通过文本匹配点击第 {row_index + 1} 个发布选项: {selector}"
                        )
                        return True
            except Exception:
                continue
        
        return False

    async def _switch_to_schedule_mode(self, page: Page):
        """
        在发布选项弹出面板中，点击"定时发布"（Programar / Schedule）tab。
        
        Meta Business Suite 的发布选项弹窗包含多个tab：
        - 第1个tab: 立即发布（Publicar ahora / Publish now）— 默认选中
        - 第2个tab: 定时发布（Programar / Schedule）
        - 第3个tab: 其他选项
        
        注意：弹窗中的 tab 可能不使用标准的 role="tab" 属性，
        而是使用普通 div/span 元素，因此需要多种策略配合。
        
        策略：
        1. JS 全面搜索：遍历最近出现的弹出层/浮层中所有元素，找包含 Programar/Schedule 的并点击
        2. Playwright 选择器文本匹配
        3. JS 兜底：在整个 document 中搜索关键词
        """
        # 等待弹窗动画完成
        await asyncio.sleep(1.5)
        
        # ========== 策略1: JS 全面搜索弹出面板中的 Programar/Schedule ==========
        try:
            js_result = await page.evaluate("""
                () => {
                    const keywords = ['programar', 'schedule', '排期', '定时发布'];
                    
                    // 查找页面中最近弹出的浮层/弹窗容器
                    // Meta Business Suite 通常使用这些容器包裹弹出内容
                    const popupSelectors = [
                        '[role="dialog"]',
                        '[role="menu"]',
                        '[role="listbox"]',
                        '[role="tablist"]',
                        '[data-testid*="popover"]',
                        '[data-testid*="dropdown"]',
                        '[data-testid*="overlay"]',
                        // Meta 常用的浮层 class 前缀
                        'div[class*="__popup"]',
                        'div[class*="popover"]',
                        'div[class*="overlay"]',
                        'div[class*="dropdown"]',
                        'div[class*="modal"]',
                    ];
                    
                    let popupContainers = [];
                    for (const sel of popupSelectors) {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                popupContainers.push(el);
                            }
                        }
                    }
                    
                    // 也查找通过 position:fixed/absolute 和高 z-index 呈现的浮层
                    const allDivs = document.querySelectorAll('div');
                    for (const div of allDivs) {
                        const style = window.getComputedStyle(div);
                        const zIndex = parseInt(style.zIndex) || 0;
                        const position = style.position;
                        if ((position === 'fixed' || position === 'absolute') && zIndex > 100) {
                            const rect = div.getBoundingClientRect();
                            if (rect.width > 50 && rect.height > 50) {
                                // 避免重复添加
                                if (!popupContainers.includes(div)) {
                                    popupContainers.push(div);
                                }
                            }
                        }
                    }
                    
                    // 在弹出容器中搜索包含关键词的叶子元素或可点击元素
                    const candidates = [];
                    for (const container of popupContainers) {
                        // 搜索所有子元素（不限于有 role 属性的）
                        const allChildren = container.querySelectorAll('*');
                        for (const el of allChildren) {
                            const text = (el.textContent || '').trim().toLowerCase();
                            const directText = el.childNodes.length === 1 && el.childNodes[0].nodeType === 3
                                ? el.childNodes[0].textContent.trim().toLowerCase()
                                : '';
                            const rect = el.getBoundingClientRect();
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            if (text.length > 100) continue;
                            
                            for (const kw of keywords) {
                                // 优先匹配直接文本节点（更精确）
                                const isDirectMatch = directText === kw;
                                const isTextMatch = text === kw;
                                const isContains = text.includes(kw) && text.length < 40;
                                
                                if (isDirectMatch || isTextMatch || isContains) {
                                    const isLeaf = el.children.length === 0;
                                    const hasRole = el.hasAttribute('role');
                                    const isClickable = el.tagName === 'BUTTON' || 
                                        el.getAttribute('role') === 'tab' ||
                                        el.getAttribute('role') === 'button' ||
                                        el.getAttribute('role') === 'radio' ||
                                        el.getAttribute('role') === 'option' ||
                                        el.getAttribute('role') === 'menuitem' ||
                                        el.hasAttribute('tabindex') ||
                                        el.style.cursor === 'pointer' ||
                                        window.getComputedStyle(el).cursor === 'pointer';
                                    
                                    candidates.push({
                                        element: el,
                                        text: text,
                                        directText: directText,
                                        tag: el.tagName,
                                        role: el.getAttribute('role') || '',
                                        isDirectMatch: isDirectMatch,
                                        isExactMatch: isTextMatch || isDirectMatch,
                                        isLeaf: isLeaf,
                                        isClickable: isClickable,
                                        hasRole: hasRole,
                                        width: rect.width,
                                        height: rect.height
                                    });
                                    break;
                                }
                            }
                        }
                    }
                    
                    // 按优先级排序
                    candidates.sort((a, b) => {
                        // 1. 直接文本节点精确匹配优先
                        if (a.isDirectMatch !== b.isDirectMatch) return a.isDirectMatch ? -1 : 1;
                        // 2. 精确匹配优先
                        if (a.isExactMatch !== b.isExactMatch) return a.isExactMatch ? -1 : 1;
                        // 3. 可点击元素优先
                        if (a.isClickable !== b.isClickable) return a.isClickable ? -1 : 1;
                        // 4. 叶子节点优先（更精确）
                        if (a.isLeaf !== b.isLeaf) return a.isLeaf ? -1 : 1;
                        // 5. 有 role 属性的优先
                        if (a.hasRole !== b.hasRole) return a.hasRole ? -1 : 1;
                        return 0;
                    });
                    
                    if (candidates.length > 0) {
                        const target = candidates[0];
                        // 如果目标是叶子 span/文本节点，向上找可点击的父元素
                        let clickEl = target.element;
                        if (target.isLeaf && !target.isClickable) {
                            const parent = clickEl.parentElement;
                            if (parent) {
                                const parentStyle = window.getComputedStyle(parent);
                                if (parentStyle.cursor === 'pointer' || parent.hasAttribute('role') || parent.hasAttribute('tabindex')) {
                                    clickEl = parent;
                                }
                            }
                        }
                        clickEl.click();
                        return {
                            success: true,
                            method: 'popup_search',
                            text: target.text,
                            directText: target.directText,
                            tag: target.tag,
                            role: target.role,
                            isDirectMatch: target.isDirectMatch,
                            totalCandidates: candidates.length,
                            totalPopups: popupContainers.length
                        };
                    }
                    
                    // 收集调试信息
                    const debugInfo = [];
                    for (const container of popupContainers) {
                        const leafNodes = container.querySelectorAll('*');
                        for (const el of leafNodes) {
                            if (el.children.length === 0) {
                                const text = (el.textContent || '').trim();
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0 && text.length > 0 && text.length < 60) {
                                    debugInfo.push(`[${el.tagName} role=${el.getAttribute('role') || 'none'}] "${text}"`);
                                }
                            }
                        }
                    }
                    
                    return {
                        success: false,
                        method: 'popup_search',
                        popupCount: popupContainers.length,
                        candidateCount: 0,
                        popupTexts: [...new Set(debugInfo)].slice(0, 40)
                    };
                }
            """)
            
            if js_result and js_result.get('success'):
                logger.info(
                    f"✅ 通过 JS 弹窗搜索切换到定时发布模式: "
                    f"方式={js_result.get('method')}, "
                    f"文本='{js_result.get('text')}', "
                    f"tag={js_result.get('tag')}, "
                    f"role={js_result.get('role', 'N/A')}, "
                    f"直接匹配={js_result.get('isDirectMatch')}, "
                    f"候选数={js_result.get('totalCandidates')}, "
                    f"弹窗数={js_result.get('totalPopups')}"
                )
                await asyncio.sleep(1)
                return
            else:
                popup_count = js_result.get('popupCount', 0) if js_result else 0
                popup_texts = js_result.get('popupTexts', []) if js_result else []
                logger.warning(
                    f"JS 弹窗搜索未找到 Programar/Schedule，"
                    f"检测到 {popup_count} 个弹出容器，"
                    f"弹窗中的叶子节点文本:\n" + "\n".join(popup_texts[:20])
                )
        except Exception as e:
            logger.warning(f"JS 弹窗搜索失败: {e}")

        # ========== 策略2: Playwright 选择器文本匹配 ==========
        schedule_mode_selectors = [
            # 精确文本匹配
            'span:text("Programar")',
            'span:text("Schedule")',
            'span:text("排期")',
            # tab 形式
            '[role="tab"]:has-text("Programar")',
            '[role="tab"]:has-text("Schedule")',
            '[role="tab"]:has-text("排期")',
            # radio/按钮形式
            'input[type="radio"][value="SCHEDULED"]',
            'label:has-text("Programar")',
            'label:has-text("Schedule")',
            'label:has-text("排期")',
            # 各种 role 形式
            'div[role="radio"]:has-text("Programar")',
            'div[role="radio"]:has-text("Schedule")',
            'div[role="option"]:has-text("Programar")',
            'div[role="option"]:has-text("Schedule")',
            'div[role="menuitem"]:has-text("Programar")',
            'div[role="menuitem"]:has-text("Schedule")',
            'div[role="menuitemradio"]:has-text("Programar")',
            'div[role="menuitemradio"]:has-text("Schedule")',
            'div[role="button"]:has-text("Programar")',
            'div[role="button"]:has-text("Schedule")',
        ]
        for selector in schedule_mode_selectors:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=1500):
                    await el.click()
                    await asyncio.sleep(1)
                    logger.info(f"✅ 已通过 Playwright 选择器切换到定时发布模式: {selector}")
                    return
            except Exception:
                continue

        # ========== 策略3: JS 全局搜索（不限于弹出容器） ==========
        try:
            js_global = await page.evaluate("""
                () => {
                    const keywords = ['programar', 'schedule', '排期'];
                    const allElements = document.querySelectorAll('*');
                    const candidates = [];
                    
                    for (const el of allElements) {
                        const text = (el.textContent || '').trim().toLowerCase();
                        const rect = el.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        if (el.children.length > 3) continue;  // 只看较小的容器/叶子
                        if (text.length > 40) continue;
                        
                        for (const kw of keywords) {
                            if (text === kw) {
                                candidates.push({
                                    element: el,
                                    text: text,
                                    tag: el.tagName,
                                    isLeaf: el.children.length === 0
                                });
                                break;
                            }
                        }
                    }
                    
                    // 叶子节点优先
                    candidates.sort((a, b) => {
                        if (a.isLeaf !== b.isLeaf) return a.isLeaf ? -1 : 1;
                        return 0;
                    });
                    
                    if (candidates.length > 0) {
                        let clickEl = candidates[0].element;
                        // 向上找可点击的父元素
                        if (candidates[0].isLeaf) {
                            let parent = clickEl.parentElement;
                            for (let i = 0; i < 3 && parent; i++) {
                                const cs = window.getComputedStyle(parent);
                                if (cs.cursor === 'pointer' || parent.hasAttribute('role') || parent.hasAttribute('tabindex')) {
                                    clickEl = parent;
                                    break;
                                }
                                parent = parent.parentElement;
                            }
                        }
                        clickEl.click();
                        return { success: true, text: candidates[0].text, tag: candidates[0].tag, count: candidates.length };
                    }
                    return { success: false };
                }
            """)
            if js_global and js_global.get('success'):
                logger.info(f"✅ 通过 JS 全局搜索切换到定时发布模式: text='{js_global.get('text')}', tag={js_global.get('tag')}")
                await asyncio.sleep(1)
                return
        except Exception as e:
            logger.warning(f"JS 全局搜索失败: {e}")
        
        logger.error("❌ 所有策略均无法将发布选项切换为定时发布（Programar/Schedule）")

    async def _fill_schedule_datetime(self, page: Page, scheduled_time: datetime):
        """
        在排期面板中填写指定的日期和时间。
        
        切换到 Programar（定时发布）tab 后，面板中会显示日期和时间输入框。
        
        Meta Business Suite 的排期面板中：
        - 日期控件格式为 "yyyy年mm月dd日"（如 "2026年03月06日"）
        - 时间控件拆分为 小时 和 分钟 两个独立的 input
        
        因此弹窗中通常有 3 个 input：日期、小时、分钟。
        
        核心策略（两步法）：
        1. 用 JS 在弹窗中定位所有 input 元素，根据值格式和 label 分类为日期/小时/分钟
        2. 用 Playwright 的鼠标点击 + 键盘输入来填写值（确保 React 能感知到变化）
        """
        # ========== 等待面板渲染完成 ==========
        await asyncio.sleep(1.5)
        
        # 准备各种日期格式
        # 日期控件实际格式为 "yyyy年mm月dd日"
        date_str_cjk = scheduled_time.strftime("%Y") + "年" + scheduled_time.strftime("%m") + "月" + scheduled_time.strftime("%d") + "日"
        date_formats = {
            'cjk': date_str_cjk,                                    # yyyy年mm月dd日
            'mdy': scheduled_time.strftime("%m/%d/%Y"),              # MM/DD/YYYY（美国）
            'dmy': scheduled_time.strftime("%d/%m/%Y"),              # DD/MM/YYYY（欧洲/拉美）
            'ymd': scheduled_time.strftime("%Y/%m/%d"),              # YYYY/MM/DD
            'iso': scheduled_time.strftime("%Y-%m-%d"),              # YYYY-MM-DD
        }
        hour_str = scheduled_time.strftime("%H")    # 小时，如 "14"
        minute_str = scheduled_time.strftime("%M")  # 分钟，如 "30"
        time_str = scheduled_time.strftime("%H:%M") # 完整时间（兜底用）
        
        logger.info(
            f"准备填写排期时间: date_cjk={date_str_cjk}, "
            f"hour={hour_str}, minute={minute_str}, "
            f"scheduled_time={scheduled_time}"
        )
        
        # ========== 第1步：JS 定位弹窗中的日期、小时、分钟 input ==========
        input_info = None
        try:
            input_info = await page.evaluate("""
                () => {
                    const result = {
                        dateInput: null,
                        hourInput: null,
                        minuteInput: null,
                        timeInput: null,
                        debugInputs: [],
                        inferredByPosition: false
                    };
                    
                    // 查找弹出面板容器
                    const popupSelectors = [
                        '[role="dialog"]',
                        '[role="menu"]',
                        '[role="listbox"]',
                        '[data-testid*="popover"]',
                        '[data-testid*="dropdown"]',
                        'div[class*="popover"]',
                        'div[class*="overlay"]',
                        'div[class*="dropdown"]',
                        'div[class*="modal"]',
                    ];
                    
                    let searchContainers = [];
                    for (const sel of popupSelectors) {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                searchContainers.push(el);
                            }
                        }
                    }
                    
                    // 也搜索高 z-index 的浮层
                    const allDivs = document.querySelectorAll('div');
                    for (const div of allDivs) {
                        const style = window.getComputedStyle(div);
                        const zIndex = parseInt(style.zIndex) || 0;
                        const position = style.position;
                        if ((position === 'fixed' || position === 'absolute') && zIndex > 100) {
                            const rect = div.getBoundingClientRect();
                            if (rect.width > 50 && rect.height > 50 && !searchContainers.includes(div)) {
                                searchContainers.push(div);
                            }
                        }
                    }
                    
                    if (searchContainers.length === 0) {
                        searchContainers = [document];
                    }
                    
                    // 在容器中搜索所有可见 input
                    let allVisibleInputs = [];
                    const seenElements = new Set();
                    for (const container of searchContainers) {
                        const inputs = container.querySelectorAll('input');
                        for (const input of inputs) {
                            if (seenElements.has(input)) continue;
                            seenElements.add(input);
                            const rect = input.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && !input.disabled) {
                                allVisibleInputs.push({
                                    element: input,
                                    type: input.type || 'text',
                                    value: input.value || '',
                                    label: input.getAttribute('aria-label') || '',
                                    placeholder: input.placeholder || '',
                                    name: input.name || '',
                                    x: Math.round(rect.x),
                                    y: Math.round(rect.y),
                                    width: Math.round(rect.width),
                                    height: Math.round(rect.height),
                                    centerX: Math.round(rect.x + rect.width / 2),
                                    centerY: Math.round(rect.y + rect.height / 2)
                                });
                            }
                        }
                    }
                    
                    // 按 y 坐标优先排序（日期通常在上方，时间在下方）
                    allVisibleInputs.sort((a, b) => a.y - b.y || a.x - b.x);
                    
                    // 收集调试信息
                    result.debugInputs = allVisibleInputs.map(item => ({
                        type: item.type,
                        value: item.value.substring(0, 40),
                        label: item.label.substring(0, 40),
                        placeholder: item.placeholder.substring(0, 30),
                        name: item.name.substring(0, 30),
                        x: item.x,
                        y: item.y,
                        width: item.width,
                        height: item.height,
                        centerX: item.centerX,
                        centerY: item.centerY
                    }));
                    
                    // ===== 分类 input：日期 / 小时 / 分钟 / 完整时间 =====
                    let dateItem = null;
                    let hourItem = null;
                    let minuteItem = null;
                    let timeItem = null;  // 如果是合并的 HH:MM 输入框
                    
                    for (const item of allVisibleInputs) {
                        const val = item.value;
                        const label = (item.label + ' ' + item.placeholder + ' ' + item.name).toLowerCase();
                        const type = item.type;
                        
                        // 判断日期输入框：包含"年"、"月"、"日"，或常见日期分隔符
                        if (!dateItem) {
                            if (type === 'date' ||
                                /[年月日]/.test(val) ||
                                /\\d{1,4}[\\/\\-]\\d{1,2}[\\/\\-]\\d{1,4}/.test(val) ||
                                /date|fecha|日期/i.test(label)) {
                                dateItem = item;
                                continue;
                            }
                        }
                        
                        // 判断小时输入框：label 包含 hour/hora/小时，或值为 1-2 位纯数字且 < 24
                        if (!hourItem) {
                            if (/hour|hora|小时|hh/i.test(label) ||
                                (type !== 'time' && /^\\d{1,2}$/.test(val) && parseInt(val) < 24 && !minuteItem)) {
                                hourItem = item;
                                continue;
                            }
                        }
                        
                        // 判断分钟输入框：label 包含 minute/minuto/分钟，或值为 1-2 位纯数字且 < 60
                        if (!minuteItem) {
                            if (/minute|minuto|分钟|mm/i.test(label) ||
                                (type !== 'time' && /^\\d{1,2}$/.test(val) && parseInt(val) < 60 && hourItem)) {
                                minuteItem = item;
                                continue;
                            }
                        }
                        
                        // 判断合并时间输入框：HH:MM 格式
                        if (!timeItem && !hourItem && !minuteItem) {
                            if (type === 'time' ||
                                /^\\d{1,2}:\\d{2}/.test(val) ||
                                /time|hora|时间/i.test(label)) {
                                timeItem = item;
                                continue;
                            }
                        }
                    }
                    
                    // ===== 按位置推断（兜底） =====
                    // 如果有 3 个 input 但未能分类，按顺序推断为：日期、小时、分钟
                    if (!dateItem && !hourItem && !minuteItem && !timeItem) {
                        if (allVisibleInputs.length >= 3) {
                            dateItem = allVisibleInputs[0];
                            hourItem = allVisibleInputs[1];
                            minuteItem = allVisibleInputs[2];
                            result.inferredByPosition = true;
                        } else if (allVisibleInputs.length === 2) {
                            dateItem = allVisibleInputs[0];
                            timeItem = allVisibleInputs[1];
                            result.inferredByPosition = true;
                        } else if (allVisibleInputs.length === 1) {
                            dateItem = allVisibleInputs[0];
                            result.inferredByPosition = true;
                        }
                    }
                    // 如果识别出日期但时间部分未分类，尝试将剩余 input 作为小时/分钟
                    if (dateItem && !hourItem && !minuteItem && !timeItem) {
                        const remaining = allVisibleInputs.filter(i => i !== dateItem);
                        if (remaining.length >= 2) {
                            hourItem = remaining[0];
                            minuteItem = remaining[1];
                        } else if (remaining.length === 1) {
                            timeItem = remaining[0];
                        }
                    }
                    
                    // ===== 确定日期格式 =====
                    if (dateItem) {
                        let dateFormat = 'cjk';  // 默认 yyyy年mm月dd日
                        const currentVal = dateItem.value;
                        if (currentVal) {
                            if (/[年月日]/.test(currentVal)) {
                                dateFormat = 'cjk';
                            } else {
                                const parts = currentVal.replace(/-/g, '/').split('/');
                                if (parts.length === 3) {
                                    if (parts[0].length === 4) {
                                        if (currentVal.includes('-')) {
                                            dateFormat = 'iso';
                                        } else {
                                            dateFormat = 'ymd';
                                        }
                                    } else {
                                        const firstNum = parseInt(parts[0]);
                                        dateFormat = firstNum > 12 ? 'dmy' : 'mdy';
                                    }
                                }
                            }
                        }
                        result.dateInput = {
                            centerX: dateItem.centerX,
                            centerY: dateItem.centerY,
                            currentValue: currentVal,
                            dateFormat: dateFormat,
                            label: dateItem.label
                        };
                    }
                    
                    if (hourItem) {
                        result.hourInput = {
                            centerX: hourItem.centerX,
                            centerY: hourItem.centerY,
                            currentValue: hourItem.value,
                            label: hourItem.label
                        };
                    }
                    
                    if (minuteItem) {
                        result.minuteInput = {
                            centerX: minuteItem.centerX,
                            centerY: minuteItem.centerY,
                            currentValue: minuteItem.value,
                            label: minuteItem.label
                        };
                    }
                    
                    if (timeItem) {
                        result.timeInput = {
                            centerX: timeItem.centerX,
                            centerY: timeItem.centerY,
                            currentValue: timeItem.value,
                            label: timeItem.label
                        };
                    }
                    
                    result.totalInputs = allVisibleInputs.length;
                    result.totalContainers = searchContainers.length;
                    return result;
                }
            """)
        except Exception as e:
            logger.error(f"❌ JS 定位日期时间 input 失败: {e}")
        
        if not input_info:
            logger.error(f"❌ 无法定位日期时间输入框，scheduled_time={scheduled_time}")
            return
        
        logger.info(
            f"JS 定位结果: dateInput={input_info.get('dateInput')}, "
            f"hourInput={input_info.get('hourInput')}, "
            f"minuteInput={input_info.get('minuteInput')}, "
            f"timeInput={input_info.get('timeInput')}, "
            f"总input数={input_info.get('totalInputs')}, "
            f"弹窗数={input_info.get('totalContainers')}"
        )
        if input_info.get('inferredByPosition'):
            logger.warning("日期/时间输入框是按位置推断的，可能不准确")
        
        has_any_input = (
            input_info.get('dateInput') or input_info.get('hourInput') or
            input_info.get('minuteInput') or input_info.get('timeInput')
        )
        if not has_any_input:
            debug_inputs = input_info.get('debugInputs', [])
            logger.error(
                f"❌ 未找到任何日期/时间输入框，弹窗中所有 input:\n" +
                "\n".join(str(inp) for inp in debug_inputs)
            )
            return
        
        # ========== 辅助函数：通过 JS nativeInputValueSetter 设值（支持 React） ==========
        async def _set_input_value_by_js(cx: int, cy: int, value: str, field_name: str, prev_val: str = ""):
            """
            通过 JS 直接设置 input 的 value，使用 nativeInputValueSetter 绕过 React 拦截，
            然后触发完整的事件链（focus/input/change/blur）确保 React 状态同步。
            
            对于 <input type="date"> 控件，value 必须是 ISO 格式 YYYY-MM-DD。
            对于普通 input，value 直接设置即可。
            """
            try:
                result = await page.evaluate("""
                    (params) => {
                        const { x, y, newValue } = params;
                        const el = document.elementFromPoint(x, y);
                        if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA')) {
                            return { success: false, error: 'elementFromPoint 未找到 input 元素', tagName: el ? el.tagName : 'null' };
                        }
                        
                        try {
                            // 聚焦
                            el.focus();
                            el.dispatchEvent(new FocusEvent('focus', { bubbles: true }));
                            
                            // 用 nativeInputValueSetter 设值（绕过 React 的 controlled input 拦截）
                            const nativeSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            ).set;
                            nativeSetter.call(el, newValue);
                            
                            // 触发完整的事件链，确保 React 感知到变化
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            
                            // 额外触发 React 16/17/18 内部事件（React 依赖 SyntheticEvent）
                            const reactPropsKey = Object.keys(el).find(key => key.startsWith('__reactProps$') || key.startsWith('__reactEvents$'));
                            if (reactPropsKey && el[reactPropsKey] && el[reactPropsKey].onChange) {
                                el[reactPropsKey].onChange({ target: el, currentTarget: el });
                            }
                            
                            // 失焦，触发 blur 事件
                            el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
                            el.blur();
                            
                            return { success: true, finalValue: el.value, inputType: el.type };
                        } catch (err) {
                            return { success: false, error: err.message };
                        }
                    }
                """, {"x": cx, "y": cy, "newValue": value})
                
                if result and result.get('success'):
                    logger.info(
                        f"✅ JS 设置{field_name}: '{value}' 成功 "
                        f"(原值: '{prev_val}', 最终值: '{result.get('finalValue')}', "
                        f"inputType: '{result.get('inputType')}', 坐标: ({cx}, {cy}))"
                    )
                    return True
                else:
                    logger.warning(f"JS 设置{field_name}失败: {result}")
                    return False
            except Exception as e:
                logger.warning(f"JS 设置{field_name}异常: {e}")
                return False
        
        # ========== 辅助函数：用 Playwright 键盘填写一个 input（兜底方案） ==========
        async def _type_into_input(cx: int, cy: int, value: str, field_name: str, prev_val: str = ""):
            """点击指定坐标的 input，清空后逐字符输入新值"""
            try:
                # 1. 点击输入框聚焦
                await page.mouse.click(cx, cy)
                await asyncio.sleep(0.3)
                
                # 2. 三击全选
                await page.mouse.click(cx, cy, click_count=3)
                await asyncio.sleep(0.2)
                
                # 3. Ctrl+A / Meta+A 双重全选
                await page.keyboard.press("Control+a")
                await asyncio.sleep(0.1)
                await page.keyboard.press("Meta+a")
                await asyncio.sleep(0.1)
                
                # 4. 删除选中内容
                await page.keyboard.press("Backspace")
                await asyncio.sleep(0.2)
                
                # 5. 逐字符输入新值
                await page.keyboard.type(value, delay=50)
                await asyncio.sleep(0.3)
                
                # 6. 按 Tab 确认输入并移出焦点
                await page.keyboard.press("Tab")
                await asyncio.sleep(0.3)
                
                logger.info(
                    f"✅ 已通过键盘输入设置{field_name}: '{value}' "
                    f"(原值: '{prev_val}', 坐标: ({cx}, {cy}))"
                )
                return True
            except Exception as e:
                logger.warning(f"键盘输入{field_name}失败: {e}")
                return False
        
        # ========== 第2步：填写日期 ==========
        # 对于 <input type="date"> 控件：
        #   - 浏览器内部存储的是 ISO 格式 YYYY-MM-DD（如 "2026-03-06"）
        #   - 显示格式取决于浏览器语言（中文→"2026年03月06日"，英文→"03/06/2026" 等）
        #   - 必须用 ISO 格式 YYYY-MM-DD 来设值，浏览器会自动转换为本地化显示
        # 对于普通 text input 控件：
        #   - 直接使用页面显示的格式（如 CJK 格式）
        date_iso = scheduled_time.strftime("%Y-%m-%d")  # ISO 格式，用于 type="date" 控件
        
        date_info = input_info.get('dateInput')
        if date_info:
            cx, cy = date_info['centerX'], date_info['centerY']
            prev_val = date_info.get('currentValue', '')
            date_format_key = date_info.get('dateFormat', 'cjk')
            
            logger.info(
                f"开始填写日期: ISO={date_iso}, "
                f"CJK={date_str_cjk}, format={date_format_key}, "
                f"原值='{prev_val}', 坐标=({cx}, {cy})"
            )
            
            # 策略1: 先用 JS + ISO 格式（适用于 type="date" 的原生日期控件）
            success = await _set_input_value_by_js(cx, cy, date_iso, "日期(ISO)", prev_val)
            
            if not success:
                # 策略2: 用 JS + 页面格式（适用于自定义日期文本输入框）
                date_str = date_formats.get(date_format_key, date_str_cjk)
                logger.info(f"策略1(JS+ISO)失败，尝试策略2(JS+{date_format_key}): {date_str}")
                success = await _set_input_value_by_js(cx, cy, date_str, f"日期({date_format_key})", prev_val)
            
            if not success:
                # 策略3: Playwright 键盘输入（兜底）
                date_str = date_formats.get(date_format_key, date_str_cjk)
                logger.info(f"策略2也失败，尝试策略3(键盘输入): {date_str}")
                success = await _type_into_input(cx, cy, date_str, "日期", prev_val)
            
            if not success:
                # 策略4: Playwright fill（最终兜底）
                logger.info("策略3也失败，尝试策略4(Playwright fill)")
                try:
                    date_selectors = [
                        'input[type="date"]',
                        'input[aria-label*="Date"]', 'input[aria-label*="Fecha"]',
                        'input[aria-label*="日期"]',
                    ]
                    for selector in date_selectors:
                        try:
                            inp = page.locator(selector).first
                            if await inp.is_visible(timeout=1000):
                                await inp.fill(date_iso)
                                await inp.press("Tab")
                                logger.info(f"✅ Playwright fill 兜底设置日期: {date_iso}")
                                success = True
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
            
            # 验证日期是否设置成功
            await asyncio.sleep(0.5)
            try:
                verify_result = await page.evaluate("""
                    (coords) => {
                        const el = document.elementFromPoint(coords.x, coords.y);
                        if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) {
                            return { value: el.value, type: el.type };
                        }
                        return null;
                    }
                """, {"x": cx, "y": cy})
                if verify_result:
                    logger.info(f"日期设置后验证: value='{verify_result.get('value')}', type='{verify_result.get('type')}'")
            except Exception:
                pass
        else:
            logger.warning("⚠️ 未找到日期输入框，跳过日期设置")
        
        # ========== 第3步：填写时间（小时+分钟 或 合并时间输入框）==========
        hour_info = input_info.get('hourInput')
        minute_info = input_info.get('minuteInput')
        time_info = input_info.get('timeInput')
        
        if hour_info and minute_info:
            # ---- 模式A：小时和分钟是两个独立的 input ----
            logger.info(f"时间输入模式: 小时+分钟分开，hour={hour_str}, minute={minute_str}")
            
            # 填写小时（先 JS，再键盘）
            cx_h, cy_h = hour_info['centerX'], hour_info['centerY']
            prev_hour = hour_info.get('currentValue', '')
            h_success = await _set_input_value_by_js(cx_h, cy_h, hour_str, "小时", prev_hour)
            if not h_success:
                await _type_into_input(cx_h, cy_h, hour_str, "小时", prev_hour)
            
            await asyncio.sleep(0.3)
            
            # 填写分钟（先 JS，再键盘）
            cx_m, cy_m = minute_info['centerX'], minute_info['centerY']
            prev_min = minute_info.get('currentValue', '')
            m_success = await _set_input_value_by_js(cx_m, cy_m, minute_str, "分钟", prev_min)
            if not m_success:
                await _type_into_input(cx_m, cy_m, minute_str, "分钟", prev_min)
            
        elif time_info:
            # ---- 模式B：合并的 HH:MM 时间输入框 ----
            logger.info(f"时间输入模式: 合并 HH:MM，time={time_str}")
            cx, cy = time_info['centerX'], time_info['centerY']
            prev_val = time_info.get('currentValue', '')
            
            success = await _set_input_value_by_js(cx, cy, time_str, "时间", prev_val)
            if not success:
                success = await _type_into_input(cx, cy, time_str, "时间", prev_val)
            if not success:
                try:
                    time_selectors = [
                        'input[type="time"]',
                        'input[aria-label*="Time"]', 'input[aria-label*="Hora"]',
                        'input[aria-label*="时间"]',
                    ]
                    for selector in time_selectors:
                        try:
                            inp = page.locator(selector).first
                            if await inp.is_visible(timeout=1000):
                                await inp.fill(time_str)
                                await inp.press("Tab")
                                logger.info(f"✅ Playwright fill 兜底设置时间: {time_str}")
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
        elif hour_info:
            # 只找到小时 input，没有分钟
            logger.warning("⚠️ 只找到小时输入框，未找到分钟输入框")
            cx_h, cy_h = hour_info['centerX'], hour_info['centerY']
            h_success = await _set_input_value_by_js(cx_h, cy_h, hour_str, "小时", hour_info.get('currentValue', ''))
            if not h_success:
                await _type_into_input(cx_h, cy_h, hour_str, "小时", hour_info.get('currentValue', ''))
        else:
            logger.warning("⚠️ 未找到任何时间输入框，跳过时间设置")
        
        await asyncio.sleep(0.5)

    async def _close_schedule_panel(self, page: Page):
        """
        关闭排期设置面板/弹窗，**必须点击"更新/Actualizar/Update"按钮确认定时发布设置**。
        
        在 Meta Business Suite 中，切换到定时发布并填写日期时间后，
        面板中会出现一个"更新"按钮（西班牙语 "Actualizar"），
        必须点击该按钮才能将发布选项从"立即发布"真正切换为"定时发布"。
        
        如果只是关闭面板（Escape/点击空白），修改不会生效。
        
        面板确认方式（按优先级）：
        1. JS 全面搜索弹窗中的确认按钮（Actualizar/Update/更新/Guardar/Save 等）
        2. Playwright 选择器匹配确认按钮
        3. 按 Escape 关闭（兜底，但修改可能不生效）
        """
        # ========== 方式1: JS 全面搜索弹窗中的确认按钮 ==========
        # 这是最可靠的方式，因为 Meta 弹窗的按钮可能使用非标准元素
        try:
            js_result = await page.evaluate("""
                () => {
                    // 确认按钮的关键词（按优先级排序）
                    // "Actualizar" 是 Meta Business Suite 西班牙语中的"更新"按钮
                    const confirmKeywords = [
                        'actualizar', 'update', '更新',
                        'guardar', 'save', '保存',
                        'aplicar', 'apply', '应用',
                        'listo', 'done', '完成',
                        'aceptar', 'ok', '确定', '确认',
                        'confirmar', 'confirm'
                    ];
                    
                    // 查找弹出面板容器
                    const popupSelectors = [
                        '[role="dialog"]',
                        '[role="menu"]',
                        '[role="listbox"]',
                        '[data-testid*="popover"]',
                        '[data-testid*="dropdown"]',
                        'div[class*="popover"]',
                        'div[class*="overlay"]',
                        'div[class*="dropdown"]',
                        'div[class*="modal"]',
                    ];
                    
                    let searchContainers = [];
                    for (const sel of popupSelectors) {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                searchContainers.push(el);
                            }
                        }
                    }
                    
                    // 也搜索高 z-index 的浮层
                    const allDivs = document.querySelectorAll('div');
                    for (const div of allDivs) {
                        const style = window.getComputedStyle(div);
                        const zIndex = parseInt(style.zIndex) || 0;
                        const position = style.position;
                        if ((position === 'fixed' || position === 'absolute') && zIndex > 100) {
                            const rect = div.getBoundingClientRect();
                            if (rect.width > 50 && rect.height > 50 && !searchContainers.includes(div)) {
                                searchContainers.push(div);
                            }
                        }
                    }
                    
                    // 如果没找到弹出容器，搜索整个 document
                    if (searchContainers.length === 0) {
                        searchContainers = [document];
                    }
                    
                    // 搜索所有可点击的确认按钮
                    const candidates = [];
                    for (const container of searchContainers) {
                        const allElements = container.querySelectorAll(
                            'button, [role="button"], a[role="button"], div[role="button"], span[role="button"], input[type="submit"]'
                        );
                        
                        for (const el of allElements) {
                            const text = (el.textContent || '').trim().toLowerCase();
                            const rect = el.getBoundingClientRect();
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            if (text.length > 40) continue;  // 排除文本太长的元素
                            
                            for (let i = 0; i < confirmKeywords.length; i++) {
                                const kw = confirmKeywords[i];
                                if (text === kw || text.includes(kw)) {
                                    candidates.push({
                                        element: el,
                                        text: text,
                                        keyword: kw,
                                        priority: i,  // 越小优先级越高
                                        tag: el.tagName,
                                        role: el.getAttribute('role') || '',
                                        x: Math.round(rect.x),
                                        y: Math.round(rect.y),
                                        width: Math.round(rect.width),
                                        isExact: text === kw
                                    });
                                    break;
                                }
                            }
                        }
                        
                        // 也搜索普通 span/div，它们可能是视觉按钮但没有 role="button"
                        const spanDivs = container.querySelectorAll('span, div');
                        for (const el of spanDivs) {
                            // 只看叶子节点或很小的容器
                            if (el.children.length > 2) continue;
                            const text = (el.textContent || '').trim().toLowerCase();
                            const rect = el.getBoundingClientRect();
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            if (text.length > 30) continue;
                            
                            const style = window.getComputedStyle(el);
                            const isClickable = style.cursor === 'pointer' || el.hasAttribute('tabindex');
                            if (!isClickable) continue;
                            
                            for (let i = 0; i < confirmKeywords.length; i++) {
                                const kw = confirmKeywords[i];
                                if (text === kw || text.includes(kw)) {
                                    // 避免与上面的结果重复
                                    const isDuplicate = candidates.some(c => 
                                        c.element === el || c.element.contains(el) || el.contains(c.element)
                                    );
                                    if (!isDuplicate) {
                                        candidates.push({
                                            element: el,
                                            text: text,
                                            keyword: kw,
                                            priority: i + 100,  // 非标准按钮优先级较低
                                            tag: el.tagName,
                                            role: 'none',
                                            x: Math.round(rect.x),
                                            y: Math.round(rect.y),
                                            width: Math.round(rect.width),
                                            isExact: text === kw
                                        });
                                    }
                                    break;
                                }
                            }
                        }
                    }
                    
                    // 按优先级排序：精确匹配 > 关键词优先级
                    candidates.sort((a, b) => {
                        if (a.isExact !== b.isExact) return a.isExact ? -1 : 1;
                        return a.priority - b.priority;
                    });
                    
                    // 收集调试信息
                    const debugInfo = candidates.map(c => ({
                        text: c.text,
                        keyword: c.keyword,
                        tag: c.tag,
                        role: c.role,
                        x: c.x,
                        y: c.y,
                        priority: c.priority
                    }));
                    
                    if (candidates.length > 0) {
                        candidates[0].element.click();
                        return {
                            success: true,
                            text: candidates[0].text,
                            keyword: candidates[0].keyword,
                            tag: candidates[0].tag,
                            role: candidates[0].role,
                            totalCandidates: candidates.length,
                            allCandidates: debugInfo.slice(0, 10)
                        };
                    }
                    
                    // 未找到按钮，返回调试信息
                    return {
                        success: false,
                        totalContainers: searchContainers.length,
                        allCandidates: debugInfo
                    };
                }
            """)
            
            if js_result and js_result.get('success'):
                logger.info(
                    f"✅ 已点击确认按钮关闭排期面板: "
                    f"文本='{js_result.get('text')}', "
                    f"关键词='{js_result.get('keyword')}', "
                    f"tag={js_result.get('tag')}, "
                    f"role={js_result.get('role')}, "
                    f"候选按钮数={js_result.get('totalCandidates')}"
                )
                await asyncio.sleep(1)
                return
            else:
                containers = js_result.get('totalContainers', 0) if js_result else 0
                all_candidates = js_result.get('allCandidates', []) if js_result else []
                logger.warning(
                    f"JS 搜索未找到确认按钮，弹出容器数={containers}，"
                    f"候选按钮:\n" + "\n".join(str(c) for c in all_candidates[:10])
                )
        except Exception as e:
            logger.warning(f"JS 搜索确认按钮失败: {e}")

        # ========== 方式2: Playwright 选择器匹配确认按钮 ==========
        close_selectors = [
            # 更新（最高优先级，Meta Business Suite 西班牙语环境的确认按钮）
            'button:has-text("Actualizar")',
            'div[role="button"]:has-text("Actualizar")',
            'button:has-text("Update")',
            'div[role="button"]:has-text("Update")',
            'button:has-text("更新")',
            # 保存
            'button:has-text("Guardar")',
            'div[role="button"]:has-text("Guardar")',
            'button:has-text("Save")',
            'div[role="button"]:has-text("Save")',
            'button:has-text("保存")',
            # 应用
            'button:has-text("Aplicar")',
            'div[role="button"]:has-text("Aplicar")',
            'button:has-text("Apply")',
            'div[role="button"]:has-text("Apply")',
            'button:has-text("应用")',
            # 完成
            'button:has-text("Listo")',
            'div[role="button"]:has-text("Listo")',
            'button:has-text("Done")',
            'div[role="button"]:has-text("Done")',
            'button:has-text("完成")',
            # 确认/接受
            'button:has-text("Aceptar")',
            'div[role="button"]:has-text("Aceptar")',
            'button:has-text("OK")',
            'button:has-text("确定")',
            'button:has-text("确认")',
            'button:has-text("Confirmar")',
            'button:has-text("Confirm")',
        ]
        for selector in close_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    await asyncio.sleep(1)
                    logger.info(f"✅ 已通过 Playwright 选择器点击确认按钮: {selector}")
                    return
            except Exception:
                continue

        # ========== 方式3: 按 Escape 关闭（兜底，但修改可能不会生效） ==========
        logger.warning("⚠️ 未找到确认按钮（Actualizar/Update/更新等），尝试 Escape 关闭面板，但修改可能不会生效")
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            logger.info("通过 Escape 键关闭排期面板（修改可能未保存）")
            return
        except Exception:
            pass

        # ========== 方式4: 点击页面安全区域（最后兜底） ==========
        try:
            viewport = page.viewport_size
            if viewport:
                safe_x = viewport['width'] // 2
                safe_y = 100
            else:
                safe_x = 600
                safe_y = 100
            await page.mouse.click(safe_x, safe_y)
            await asyncio.sleep(0.5)
            logger.info(f"通过点击安全区域({safe_x}, {safe_y})关闭排期面板（修改可能未保存）")
        except Exception:
            pass

    async def _set_scheduled_times_via_js(self, page: Page, videos: List[TaskVideo]):
        """
        通过 JavaScript 方式在 Bulk upload 页面为每个视频设置定时发布时间。
        
        当标准 Playwright selector 无法定位排期选项时的兜底方案：
        遍历页面上的视频行，找到每行中与排期相关的可交互元素并逐一设置。
        """
        for i, video in enumerate(videos):
            if not video.scheduled_time:
                continue
            
            scheduled_time = video.scheduled_time
            date_str = scheduled_time.strftime("%Y/%m/%d")
            time_str = scheduled_time.strftime("%H:%M")
            
            try:
                # 通过 JS 找到并点击第 i 个视频的排期选项
                clicked = await page.evaluate(f"""
                    (index) => {{
                        // 查找所有可能的视频条目行
                        const rows = document.querySelectorAll(
                            'div[role="row"], div[role="listitem"], tr, div[data-testid*="video"], div[data-testid*="reel"]'
                        );
                        
                        // 如果没有找到行，尝试查找所有排期相关的下拉/按钮
                        const scheduleButtons = [];
                        const allElements = document.querySelectorAll(
                            'div[role="button"], button, select, div[aria-haspopup]'
                        );
                        for (const el of allElements) {{
                            const text = (el.textContent || '').trim().toLowerCase();
                            const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
                            const publishKeywords = [
                                'publish now', 'schedule', '立即发布', '排期', 'publicar ahora', 'programar',
                                'publish option', 'publishing option', '发布选项'
                            ];
                            for (const kw of publishKeywords) {{
                                if (text.includes(kw) || ariaLabel.includes(kw)) {{
                                    const rect = el.getBoundingClientRect();
                                    if (rect.width > 0 && rect.height > 0) {{
                                        scheduleButtons.push(el);
                                    }}
                                    break;
                                }}
                            }}
                        }}
                        
                        if (scheduleButtons.length > index) {{
                            scheduleButtons[index].click();
                            return {{ found: true, type: 'schedule_button', total: scheduleButtons.length }};
                        }}
                        
                        if (rows.length > index) {{
                            // 在对应行中查找排期相关元素
                            const row = rows[index];
                            const rowButtons = row.querySelectorAll(
                                'div[role="button"], button, select, div[aria-haspopup]'
                            );
                            for (const btn of rowButtons) {{
                                const text = (btn.textContent || '').trim().toLowerCase();
                                if (text.includes('publish') || text.includes('schedule') || 
                                    text.includes('发布') || text.includes('排期') ||
                                    text.includes('publicar') || text.includes('programar')) {{
                                    btn.click();
                                    return {{ found: true, type: 'row_button' }};
                                }}
                            }}
                        }}
                        
                        return {{ found: false, rows: rows.length, buttons: scheduleButtons.length }};
                    }}
                """, i)
                
                if clicked and clicked.get('found'):
                    await asyncio.sleep(1)
                    
                    # 切换到定时发布模式
                    await self._switch_to_schedule_mode(page)
                    
                    # 填写日期和时间
                    await self._fill_schedule_datetime(page, scheduled_time)
                    
                    # 关闭面板
                    await self._close_schedule_panel(page)
                    await asyncio.sleep(0.5)
                    
                    logger.info(
                        f"视频 {i + 1}/{len(videos)} ({video.file_name}) "
                        f"通过 JS 方式设置定时发布: {scheduled_time}"
                    )
                else:
                    logger.warning(
                        f"视频 {i + 1}/{len(videos)} ({video.file_name}) "
                        f"未找到排期选项: {clicked}"
                    )
            except Exception as e:
                logger.warning(
                    f"视频 {i + 1}/{len(videos)} ({video.file_name}) "
                    f"JS 方式设置排期失败: {e}"
                )
                continue

    async def _set_scheduled_time(self, page: Page, scheduled_time: datetime):
        """
        在 Meta Business Suite 中设置全局定时发布时间（兼容旧逻辑）。
        
        注意：此方法已被 _set_scheduled_times_per_video 替代用于批量发布场景。
        保留此方法以供单视频发布场景或兼容使用。
        
        Meta Business Suite 的排期流程：
        1. 点击 "Schedule" / "排期" 按钮（或展开发布选项）
        2. 设置日期和时间
        """
        try:
            # 步骤1: 切换到定时发布模式
            await self._switch_to_schedule_mode(page)

            # 步骤2: 填写日期和时间
            await self._fill_schedule_datetime(page, scheduled_time)

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

        # ========== 策略2: 通过 JavaScript 全面搜索并点击 checkbox ==========
        # Meta Business Suite 的 checkbox 可能是:
        # - 标准 input[type="checkbox"]（可能隐藏，实际点击 label）
        # - div/span[role="checkbox"]
        # - 自定义组件（无 role，使用 aria-checked 或 data-* 属性）
        # - 纯 CSS 模拟的 checkbox（通过父元素的 class 变化体现选中状态）
        try:
            selected = await page.evaluate("""
                () => {
                    const result = { method: 'none', count: 0, debug: [] };
                    
                    // ========== 阶段A: 搜索所有可能的 checkbox 元素 ==========
                    const checkboxCandidates = [];
                    
                    // A1: 标准 checkbox（包括隐藏的）
                    const standardCbs = document.querySelectorAll('input[type="checkbox"]');
                    for (const cb of standardCbs) {
                        // 对于隐藏的 input，找到其关联的 label 作为点击目标
                        const rect = cb.getBoundingClientRect();
                        let clickTarget = cb;
                        let isHidden = (rect.width === 0 || rect.height === 0);
                        
                        if (isHidden) {
                            // 查找关联的 label
                            const id = cb.id;
                            let label = null;
                            if (id) {
                                label = document.querySelector(`label[for="${id}"]`);
                            }
                            if (!label) {
                                label = cb.closest('label');
                            }
                            if (!label) {
                                // 查找最近的可见兄弟/父元素
                                const parent = cb.parentElement;
                                if (parent) {
                                    const siblings = parent.children;
                                    for (const sib of siblings) {
                                        if (sib !== cb) {
                                            const sibRect = sib.getBoundingClientRect();
                                            if (sibRect.width > 0 && sibRect.height > 0) {
                                                label = sib;
                                                break;
                                            }
                                        }
                                    }
                                }
                            }
                            if (label) {
                                clickTarget = label;
                                isHidden = false;
                            }
                        }
                        
                        const targetRect = clickTarget.getBoundingClientRect();
                        if (targetRect.width > 0 && targetRect.height > 0) {
                            checkboxCandidates.push({
                                element: clickTarget,
                                inputElement: cb,
                                type: 'input_checkbox',
                                isChecked: cb.checked || cb.getAttribute('aria-checked') === 'true',
                                ariaLabel: (cb.getAttribute('aria-label') || clickTarget.getAttribute('aria-label') || '').toLowerCase(),
                                text: (clickTarget.textContent || '').trim().substring(0, 50).toLowerCase(),
                                x: targetRect.x,
                                y: targetRect.y,
                                width: targetRect.width,
                                height: targetRect.height
                            });
                        }
                    }
                    
                    // A2: role="checkbox" 元素
                    const roleCbs = document.querySelectorAll('[role="checkbox"]');
                    for (const cb of roleCbs) {
                        const rect = cb.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            // 避免与标准 checkbox 重复
                            if (cb.tagName === 'INPUT') continue;
                            checkboxCandidates.push({
                                element: cb,
                                inputElement: null,
                                type: 'role_checkbox',
                                isChecked: cb.getAttribute('aria-checked') === 'true',
                                ariaLabel: (cb.getAttribute('aria-label') || '').toLowerCase(),
                                text: (cb.textContent || '').trim().substring(0, 50).toLowerCase(),
                                x: rect.x,
                                y: rect.y,
                                width: rect.width,
                                height: rect.height
                            });
                        }
                    }
                    
                    // A3: 搜索带有 aria-checked 属性的任何元素
                    const ariaCheckedEls = document.querySelectorAll('[aria-checked]');
                    for (const el of ariaCheckedEls) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            // 避免重复
                            const isDuplicate = checkboxCandidates.some(c => c.element === el);
                            if (!isDuplicate) {
                                checkboxCandidates.push({
                                    element: el,
                                    inputElement: null,
                                    type: 'aria_checked',
                                    isChecked: el.getAttribute('aria-checked') === 'true',
                                    ariaLabel: (el.getAttribute('aria-label') || '').toLowerCase(),
                                    text: (el.textContent || '').trim().substring(0, 50).toLowerCase(),
                                    x: rect.x,
                                    y: rect.y,
                                    width: rect.width,
                                    height: rect.height
                                });
                            }
                        }
                    }
                    
                    // A4: 搜索视觉上看起来像 checkbox 的小方块元素（尺寸约 16-30px）
                    // 这些通常在表格/列表的第一列，用于选择行
                    const smallSquares = document.querySelectorAll('div, span, i, svg');
                    for (const el of smallSquares) {
                        const rect = el.getBoundingClientRect();
                        const w = rect.width;
                        const h = rect.height;
                        // checkbox 尺寸通常在 14-36px 之间，且近似方形
                        if (w >= 14 && w <= 36 && h >= 14 && h <= 36 && Math.abs(w - h) < 6) {
                            const style = window.getComputedStyle(el);
                            // 有边框或背景色的小方块可能是 checkbox
                            const hasBorder = style.borderWidth && parseFloat(style.borderWidth) > 0;
                            const hasRadius = style.borderRadius && parseFloat(style.borderRadius) >= 0;
                            const hasBg = style.backgroundColor && style.backgroundColor !== 'rgba(0, 0, 0, 0)' && style.backgroundColor !== 'transparent';
                            const isSvg = el.tagName === 'svg' || el.tagName === 'SVG';
                            
                            if ((hasBorder || hasBg || isSvg) && el.children.length <= 3) {
                                const isDuplicate = checkboxCandidates.some(c => 
                                    c.element === el || c.element.contains(el) || el.contains(c.element)
                                );
                                if (!isDuplicate) {
                                    // 检查是否在表格/列表行的左侧区域（通常第1列）
                                    const parentRow = el.closest('div[role="row"], tr, [role="listitem"]');
                                    checkboxCandidates.push({
                                        element: el,
                                        inputElement: null,
                                        type: 'visual_checkbox',
                                        isChecked: false,  // 无法确定
                                        ariaLabel: (el.getAttribute('aria-label') || '').toLowerCase(),
                                        text: '',
                                        x: rect.x,
                                        y: rect.y,
                                        width: w,
                                        height: h,
                                        inRow: !!parentRow
                                    });
                                }
                            }
                        }
                    }
                    
                    // 收集调试信息
                    for (const c of checkboxCandidates) {
                        result.debug.push({
                            type: c.type,
                            isChecked: c.isChecked,
                            ariaLabel: c.ariaLabel.substring(0, 40),
                            text: c.text.substring(0, 40),
                            x: Math.round(c.x),
                            y: Math.round(c.y),
                            w: Math.round(c.width),
                            h: Math.round(c.height),
                            inRow: c.inRow || false
                        });
                    }
                    
                    // ========== 阶段B: 先尝试全选 ==========
                    const selectAllKeywords = ['select all', '全选', 'seleccionar todo', 'marcar todo'];
                    for (const c of checkboxCandidates) {
                        for (const kw of selectAllKeywords) {
                            if (c.ariaLabel.includes(kw) || c.text.includes(kw)) {
                                c.element.click();
                                result.method = 'js_select_all';
                                result.count = 1;
                                result.matchedLabel = c.ariaLabel || c.text;
                                return result;
                            }
                        }
                    }
                    
                    // ========== 阶段C: 逐个勾选非全选的 checkbox ==========
                    // 优先使用标准 checkbox 和 role checkbox
                    const priorityTypes = ['input_checkbox', 'role_checkbox', 'aria_checked'];
                    let clickedCount = 0;
                    
                    for (const pType of priorityTypes) {
                        const candidates = checkboxCandidates.filter(c => c.type === pType);
                        if (candidates.length > 0) {
                            for (const c of candidates) {
                                if (!c.isChecked) {
                                    c.element.click();
                                    clickedCount++;
                                }
                            }
                            if (clickedCount > 0 || candidates.length > 0) {
                                result.method = 'js_individual_' + pType;
                                result.count = clickedCount;
                                result.totalFound = candidates.length;
                                return result;
                            }
                        }
                    }
                    
                    // ========== 阶段D: 如果以上都无结果，尝试视觉 checkbox ==========
                    // 只点击在表格行内的视觉 checkbox
                    const visualInRow = checkboxCandidates.filter(
                        c => c.type === 'visual_checkbox' && c.inRow
                    );
                    if (visualInRow.length > 0) {
                        for (const c of visualInRow) {
                            c.element.click();
                            clickedCount++;
                        }
                        result.method = 'js_visual_in_row';
                        result.count = clickedCount;
                        return result;
                    }
                    
                    result.method = 'none_found';
                    result.totalCandidates = checkboxCandidates.length;
                    return result;
                }
            """)
            
            method = selected.get('method', 'none')
            count = selected.get('count', 0)
            debug_info = selected.get('debug', [])
            
            logger.info(
                f"通过 JS 勾选视频 checkbox: 方式={method}, "
                f"数量={count}, 找到候选项={len(debug_info)}"
            )
            
            if debug_info:
                logger.debug(
                    f"所有 checkbox 候选项:\n" +
                    "\n".join(str(d) for d in debug_info[:20])
                )
            
            await asyncio.sleep(1)

            # 验证勾选状态
            if count == 0:
                logger.warning(
                    f"未能勾选任何 checkbox（候选项数: {len(debug_info)}），"
                    f"尝试 Playwright 逐个点击..."
                )
                
                # 兜底方案A：更广泛的选择器
                broader_selectors = [
                    'input[type="checkbox"]',
                    '[role="checkbox"]',
                    '[aria-checked]',
                    'label:has(input[type="checkbox"])',
                ]
                clicked_any = False
                for selector in broader_selectors:
                    try:
                        elements = page.locator(selector)
                        el_count = await elements.count()
                        if el_count > 0:
                            logger.info(f"Playwright 找到 {el_count} 个 '{selector}' 元素，逐个点击...")
                            for i in range(el_count):
                                try:
                                    el = elements.nth(i)
                                    if await el.is_visible(timeout=1000):
                                        await el.click(force=True)
                                        await asyncio.sleep(0.3)
                                        clicked_any = True
                                except Exception:
                                    continue
                            if clicked_any:
                                logger.info(f"通过 Playwright '{selector}' 勾选了视频")
                                break
                    except Exception:
                        continue
                
                # 兜底方案B：如果上面都不行，截图打印页面DOM信息以便调试
                if not clicked_any:
                    dom_info = await page.evaluate("""
                        () => {
                            // 打印页面中所有可交互元素的摘要
                            const summary = [];
                            const interactiveEls = document.querySelectorAll(
                                'input, [role="checkbox"], [role="radio"], [role="switch"], [aria-checked], label'
                            );
                            for (const el of interactiveEls) {
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {
                                    summary.push({
                                        tag: el.tagName,
                                        type: el.type || '',
                                        role: el.getAttribute('role') || '',
                                        ariaChecked: el.getAttribute('aria-checked') || '',
                                        ariaLabel: (el.getAttribute('aria-label') || '').substring(0, 40),
                                        text: (el.textContent || '').trim().substring(0, 40),
                                        x: Math.round(rect.x),
                                        y: Math.round(rect.y),
                                        w: Math.round(rect.width),
                                        h: Math.round(rect.height)
                                    });
                                }
                            }
                            return summary.slice(0, 30);
                        }
                    """)
                    logger.warning(
                        f"所有兜底方案均未找到 checkbox，页面可交互元素摘要:\n" +
                        "\n".join(str(d) for d in (dom_info or []))
                    )

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
        在 Meta Business Suite Bulk upload 页面中，点击右下角的发布/排期按钮。
        
        重要：必须精确定位右下角的发布按钮，避免误点击左侧导航栏的链接。
        
        Bulk upload reels 页面的发布按钮可能有多种形式：
        - "Publicar todo" / "Publish all" / "全部发布" 按钮（批量发布）
        - "Programar todo" / "Schedule all" / "全部排期" 按钮（批量排期）
        - "Publicar" / "Publish" / "发布" 按钮
        - "Programar" / "Schedule" / "排期" 按钮
        """
        # ========== 优先使用 JS 在右下角区域精确查找发布按钮 ==========
        try:
            js_result = await page.evaluate("""
                () => {
                    const viewportWidth = window.innerWidth;
                    const viewportHeight = window.innerHeight;
                    
                    // 发布按钮关键词（按优先级排列：批量 > 单个，排期 > 发布）
                    const publishTexts = [
                        'programar todo', 'schedule all', '全部排期',
                        'publicar todo', 'publish all', '全部发布',
                        'programar', 'schedule', '排期',
                        'publicar', 'publish', '发布'
                    ];
                    
                    // 搜索范围：button 和 div[role="button"]
                    const allButtons = document.querySelectorAll('button, div[role="button"], a[role="button"]');
                    const candidates = [];
                    
                    for (const btn of allButtons) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        
                        const text = (btn.textContent || '').trim().toLowerCase();
                        if (text.length > 60) continue;
                        
                        // 检查是否在页面右半部分且偏下方（右下角区域）
                        const isRightArea = rect.left > viewportWidth * 0.4;
                        const isBottomArea = rect.top > viewportHeight * 0.5;
                        const isRightBottom = isRightArea && isBottomArea;
                        
                        for (let i = 0; i < publishTexts.length; i++) {
                            const kw = publishTexts[i];
                            if (text === kw || text.includes(kw)) {
                                candidates.push({
                                    element: btn,
                                    text: text,
                                    tag: btn.tagName,
                                    role: btn.getAttribute('role') || '',
                                    priority: i,  // 关键词优先级
                                    isRightBottom: isRightBottom,
                                    isEnabled: !btn.disabled && !btn.getAttribute('aria-disabled'),
                                    x: Math.round(rect.left),
                                    y: Math.round(rect.top),
                                    width: Math.round(rect.width),
                                    height: Math.round(rect.height)
                                });
                                break;
                            }
                        }
                    }
                    
                    // 按优先级排序：
                    // 1. 右下角区域优先（避免点击左侧导航链接）
                    // 2. 关键词优先级（批量 > 单个）
                    // 3. 可用状态优先
                    candidates.sort((a, b) => {
                        if (a.isRightBottom !== b.isRightBottom) return a.isRightBottom ? -1 : 1;
                        if (a.isEnabled !== b.isEnabled) return a.isEnabled ? -1 : 1;
                        return a.priority - b.priority;
                    });
                    
                    if (candidates.length > 0) {
                        const target = candidates[0];
                        target.element.click();
                        return {
                            found: true,
                            text: target.text,
                            tag: target.tag,
                            role: target.role,
                            isRightBottom: target.isRightBottom,
                            position: `(${target.x}, ${target.y})`,
                            totalCandidates: candidates.length
                        };
                    }
                    
                    // 调试信息
                    const debugButtons = [];
                    for (const btn of allButtons) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            const text = (btn.textContent || '').trim();
                            if (text.length > 0 && text.length < 60) {
                                debugButtons.push({
                                    text: text,
                                    tag: btn.tagName,
                                    x: Math.round(rect.left),
                                    y: Math.round(rect.top),
                                    role: btn.getAttribute('role') || 'none'
                                });
                            }
                        }
                    }
                    
                    return {
                        found: false,
                        buttons: debugButtons.slice(0, 30)
                    };
                }
            """)
            
            if js_result and js_result.get('found'):
                logger.info(
                    f"✅ 已点击发布按钮: text='{js_result.get('text')}', "
                    f"tag={js_result.get('tag')}, position={js_result.get('position')}, "
                    f"右下角={js_result.get('isRightBottom')}, 候选数={js_result.get('totalCandidates')}"
                )
                await asyncio.sleep(3)

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
            else:
                debug_buttons = js_result.get('buttons', []) if js_result else []
                logger.warning(
                    f"JS 未找到发布按钮，页面上的按钮:\n" +
                    "\n".join(str(b) for b in debug_buttons[:15])
                )
        except Exception as e:
            logger.warning(f"JS 查找发布按钮失败: {e}")

        # ========== 备选：Playwright 选择器（限定右下角区域） ==========
        publish_selectors = [
            'button:has-text("Programar todo")',
            'button:has-text("Schedule all")',
            'button:has-text("全部排期")',
            'button:has-text("Publicar todo")',
            'button:has-text("Publish all")',
            'button:has-text("全部发布")',
            'button:has-text("Programar")',
            'button:has-text("Schedule")',
            'button:has-text("排期")',
            'button:has-text("Publicar")',
            'button:has-text("Publish")',
            'button:has-text("发布")',
        ]
        for selector in publish_selectors:
            try:
                elements = page.locator(selector)
                count = await elements.count()
                for idx in range(count):
                    btn = elements.nth(idx)
                    if await btn.is_visible(timeout=2000) and await btn.is_enabled(timeout=1000):
                        # 检查按钮位置是否在右半部分（避免左侧导航）
                        box = await btn.bounding_box()
                        if box and box['x'] > 200:  # 排除左侧导航栏中的按钮
                            await btn.click()
                            await asyncio.sleep(3)
                            logger.info(f"已通过 Playwright 点击发布按钮: {selector}, 位置 x={box['x']:.0f}")

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
            except Exception:
                continue

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

    async def retry_failed_videos(
        self,
        task_id: str,
        page_name: str,
        video_ids: List[str],
    ) -> dict:
        """
        重试某个公共主页下指定的视频子任务。
        
        流程：
        1. 启动浏览器并登录
        2. 切换到目标主页
        3. 仅发布指定的视频子任务
        4. 更新状态
        5. 重新推断主任务状态
        
        Args:
            task_id: 任务ID
            page_name: 公共主页名称
            video_ids: 需要重试的视频子任务ID列表
        """
        task = await self.task_service.get_task(task_id)
        if not task:
            return {"success": False, "message": "任务不存在"}

        # 获取账号和主页信息
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

        # 找到目标主页
        fb_page = None
        for p in account.pages:
            if p.page_name == page_name:
                fb_page = p
                break
        if not fb_page:
            return {"success": False, "message": f"未找到主页: {page_name}"}

        # 获取该主页下需要重试的视频子任务
        page_videos = await self.task_service.get_videos_by_page(task_id, page_name)
        retry_videos = [v for v in page_videos if v.id in video_ids]
        if not retry_videos:
            return {"success": False, "message": "没有需要重试的视频子任务"}

        # 将这些视频状态标记为上传中
        for v in retry_videos:
            await self.task_service.update_video_status(v.id, VideoStatus.UPLOADING)

        logger.info(
            f"开始重试发布: task={task.task_name}, page={page_name}, "
            f"视频数量={len(retry_videos)}"
        )

        try:
            # 步骤1: 启动浏览器并登录
            login_result = await self.browser_manager.login_facebook(
                account.id, wait_for_auth=True
            )
            if not login_result["success"]:
                # 登录失败，更新视频状态
                for v in retry_videos:
                    await self.task_service.update_video_status(
                        v.id, VideoStatus.FAILED, f"登录失败: {login_result['message']}"
                    )
                await self.task_service.finalize_task_status(task_id)
                return {"success": False, "message": f"登录失败: {login_result['message']}"}

            page = await self.browser_manager.get_page(account.id)
            if not page:
                for v in retry_videos:
                    await self.task_service.update_video_status(
                        v.id, VideoStatus.FAILED, "无法获取浏览器页面"
                    )
                await self.task_service.finalize_task_status(task_id)
                return {"success": False, "message": "无法获取浏览器页面"}

            # 步骤2: 切换到目标主页
            await self._switch_to_page(page, fb_page)
            await asyncio.sleep(random.uniform(1, 3))

            # 步骤3: 批量发布该主页下的视频
            batch_result = await self._publish_videos_batch(
                page=page,
                task=task,
                videos=retry_videos,
                fb_page=fb_page,
                account=account,
            )

            # 步骤4: 重新推断主任务状态
            final_status = await self.task_service.finalize_task_status(task_id)

            return {
                "success": True,
                "message": (
                    f"主页 {page_name} 重试完成: "
                    f"成功 {batch_result['success_count']}, "
                    f"失败 {batch_result['fail_count']}"
                ),
                "task_status": final_status.value,
            }

        except Exception as e:
            logger.error(f"重试发布异常: {e}")
            # 将重试的视频标记为失败
            for v in retry_videos:
                await self.task_service.update_video_status(
                    v.id, VideoStatus.FAILED, str(e)
                )
            await self.task_service.finalize_task_status(task_id)
            return {"success": False, "message": f"重试失败: {str(e)}"}
