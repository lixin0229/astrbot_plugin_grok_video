import asyncio
import json
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin

import httpx
from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from .utils.file_send_server import send_file
except ImportError:
    plugin_dir = Path(__file__).parent
    plugin_dir_str = str(plugin_dir)
    if plugin_dir_str not in sys.path:
        sys.path.append(plugin_dir_str)
    try:
        from utils.file_send_server import send_file  # type: ignore
    except ImportError:
        send_file = None
        logger.warning("NapCat 文件转发模块未找到，将跳过 NapCat 中转功能")


@register("grok-video", "Claude", "Grok视频生成插件，支持根据图片和提示词生成视频", "1.0.0")
class GrokVideoPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # API配置
        self.server_url = config.get("server_url", "https://api.x.ai").rstrip('/')
        self.model_id = config.get("model_id", "grok-imagine-0.9")
        self.api_key = config.get("api_key", "")
        self.enabled = config.get("enabled", True)
        
        # 请求配置
        self.timeout_seconds = config.get("timeout_seconds", 180)
        self.max_retry_attempts = config.get("max_retry_attempts", 3)
        
        # 群组控制
        self.group_control_mode = config.get("group_control_mode", "off").lower()
        self.group_list = list(config.get("group_list", []))
        
        # 速率限制
        self.rate_limit_enabled = config.get("rate_limit_enabled", True)
        self.rate_limit_window_seconds = config.get("rate_limit_window_seconds", 3600)
        self.rate_limit_max_calls = config.get("rate_limit_max_calls", 5)
        self._rate_limit_bucket = {}  # group_id -> {"window_start": float, "count": int}
        
        # 管理员用户
        self.admin_users = config.get("admin_users", [])

        self.nap_server_address = (config.get("nap_server_address") or "").strip()
        nap_port = config.get("nap_server_port")
        try:
            self.nap_server_port = int(nap_port)
        except (TypeError, ValueError):
            self.nap_server_port = 0

        self.save_video_enabled = config.get("save_video_enabled", False)

        # 使用 AstrBot data 目录保存视频，确保 NapCat 可访问
        plugin_data_dir = Path(StarTools.get_data_dir("astrbot_plugin_grok_video"))
        self.videos_dir = plugin_data_dir / "videos"
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        self.videos_dir = self.videos_dir.resolve()
        
        # 构建完整的API URL
        self.api_url = urljoin(self.server_url + "/", "v1/chat/completions")
        
        logger.info(f"Grok视频生成插件已初始化，API地址: {self.api_url}")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查是否为管理员"""
        return str(event.get_sender_id()) in [str(u) for u in self.admin_users]

    def _get_callback_api_base(self) -> Optional[str]:
        """读取 AstrBot 全局 callback_api_base 配置"""
        try:
            config = self.context.get_config()
            if isinstance(config, dict):
                return config.get("callback_api_base")
        except Exception as e:
            logger.debug(f"读取 callback_api_base 失败: {e}")
        return None

    def _check_group_access(self, event: AstrMessageEvent) -> Optional[str]:
        """检查群组访问权限和速率限制"""
        try:
            group_id = None
            try:
                group_id = event.get_group_id()
            except Exception:
                group_id = None

            # 群组白名单/黑名单检查
            if group_id:
                if self.group_control_mode == "whitelist" and group_id not in self.group_list:
                    return "当前群组未被授权使用视频生成功能"
                if self.group_control_mode == "blacklist" and group_id in self.group_list:
                    return "当前群组已被限制使用视频生成功能"

                # 速率限制检查（仅对群组）
                if self.rate_limit_enabled:
                    now = time.time()
                    bucket = self._rate_limit_bucket.get(group_id, {"window_start": now, "count": 0})
                    window_start = bucket.get("window_start", now)
                    count = int(bucket.get("count", 0))
                    
                    if now - window_start >= self.rate_limit_window_seconds:
                        window_start = now
                        count = 0
                    
                    if count >= self.rate_limit_max_calls:
                        return f"本群调用已达上限（{self.rate_limit_max_calls}次/{self.rate_limit_window_seconds}秒），请稍后再试"
                    
                    # 预占位+1
                    bucket["window_start"], bucket["count"] = window_start, count + 1
                    self._rate_limit_bucket[group_id] = bucket

        except Exception as e:
            logger.error(f"群组访问检查失败: {e}")
            return None
        
        return None

    async def _extract_images_from_message(self, event: AstrMessageEvent) -> List[str]:
        """从消息中提取图片的base64数据"""
        images = []
        
        if hasattr(event, 'message_obj') and event.message_obj and hasattr(event.message_obj, 'message'):
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    try:
                        base64_data = await comp.convert_to_base64()
                        if base64_data:
                            # 确保是完整的 data URL 格式
                            if not base64_data.startswith('data:'):
                                base64_data = f"data:image/jpeg;base64,{base64_data}"
                            images.append(base64_data)
                    except Exception as e:
                        logger.warning(f"图片转base64失败: {e}")
                elif isinstance(comp, Reply) and comp.chain:
                    # 检查引用消息中的图片
                    for reply_comp in comp.chain:
                        if isinstance(reply_comp, Image):
                            try:
                                base64_data = await reply_comp.convert_to_base64()
                                if base64_data:
                                    # 确保是完整的 data URL 格式
                                    if not base64_data.startswith('data:'):
                                        base64_data = f"data:image/jpeg;base64,{base64_data}"
                                    images.append(base64_data)
                            except Exception as e:
                                logger.warning(f"引用图片转base64失败: {e}")
        
        return images

    async def _call_grok_api(self, prompt: str, image_base64: str) -> Tuple[Optional[str], Optional[str]]:
        """调用Grok API生成视频"""
        if not self.api_key:
            return None, "未配置API密钥"
        
        # 构建请求数据
        payload = {
            "model": self.model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_base64
                            }
                        }
                    ]
                }
            ]
        }
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        timeout_config = httpx.Timeout(
            connect=10.0,
            read=self.timeout_seconds,
            write=10.0,
            pool=self.timeout_seconds + 10
        )
        
        for attempt in range(self.max_retry_attempts):
            try:
                logger.info(f"调用Grok API (尝试 {attempt + 1}/{self.max_retry_attempts})")
                logger.debug(f"请求URL: {self.api_url}")
                logger.debug(f"请求模型: {self.model_id}")
                
                async with httpx.AsyncClient(timeout=timeout_config) as client:
                    response = await client.post(
                        self.api_url,
                        json=payload,
                        headers=headers
                    )
                    
                    logger.info(f"API响应状态码: {response.status_code}")
                    
                    # 记录响应内容用于调试
                    response_text = response.text
                    logger.debug(f"API响应内容: {response_text[:500]}...")
                    
                    if response.status_code == 200:
                        try:
                            result = response.json()
                            logger.debug(f"解析的JSON响应: {result}")
                            
                            # 解析响应获取视频URL
                            if "choices" in result and len(result["choices"]) > 0:
                                content = result["choices"][0].get("message", {}).get("content", "")
                                logger.info(f"API返回内容: {content}")
                                
                                # 查找视频标签
                                if "<video" in content and "src=" in content:
                                    # 提取视频URL
                                    video_match = re.search(r'src="([^"]+)"', content)
                                    if video_match:
                                        video_url = video_match.group(1)
                                        logger.info(f"提取到视频URL: {video_url}")
                                        return video_url, None
                                    else:
                                        return None, "无法从响应中提取视频URL"
                                else:
                                    return None, f"API响应中未包含视频内容: {content}"
                            else:
                                return None, f"API响应格式错误: {result}"
                        except json.JSONDecodeError as e:
                            return None, f"API响应JSON解析失败: {str(e)}, 响应内容: {response_text[:200]}"
                    
                    elif response.status_code == 403:
                        return None, "API访问被拒绝，请检查密钥和权限"
                    
                    else:
                        error_msg = f"API请求失败 (状态码: {response.status_code})"
                        try:
                            error_detail = response.json()
                            logger.debug(f"错误详情JSON: {error_detail}")
                            if "error" in error_detail:
                                error_msg += f": {error_detail['error']}"
                            elif "message" in error_detail:
                                error_msg += f": {error_detail['message']}"
                            else:
                                error_msg += f": {error_detail}"
                        except:
                            error_msg += f": {response_text[:200]}"
                        
                        if attempt == self.max_retry_attempts - 1:
                            return None, error_msg
                        
                        logger.warning(f"{error_msg}，等待重试...")
                        await asyncio.sleep(2)  # 增加重试间隔
            
            except httpx.TimeoutException:
                error_msg = f"请求超时 ({self.timeout_seconds}秒)"
                if attempt == self.max_retry_attempts - 1:
                    return None, error_msg
                logger.warning(f"{error_msg}，等待重试...")
                await asyncio.sleep(1)
            
            except Exception as e:
                error_msg = f"请求异常: {str(e)}"
                if attempt == self.max_retry_attempts - 1:
                    return None, error_msg
                logger.warning(f"{error_msg}，等待重试...")
                await asyncio.sleep(1)
        
        return None, "所有重试均失败"

    async def _download_video(self, video_url: str) -> Optional[str]:
        """下载视频到本地"""
        try:
            filename = f"grok_video_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.mp4"
            file_path = self.videos_dir / filename
            
            timeout_config = httpx.Timeout(
                connect=10.0,
                read=300.0,  # 视频文件可能较大，给更长的读取时间
                write=10.0,
                pool=300.0
            )
            
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                response = await client.get(video_url)
                response.raise_for_status()
                
                # 保存视频文件
                with open(file_path, 'wb') as f:
                    f.write(response.content)
                
                # 确保返回绝对路径，避免路径问题
                absolute_path = file_path.resolve()
                logger.info(f"视频已保存到: {absolute_path}")
                return str(absolute_path)
        
        except Exception as e:
            logger.error(f"下载视频失败: {e}")
            return None

    async def _prepare_video_path(self, video_path: str) -> str:
        """Optionally bridge the video file through NapCat so the client can access it."""
        if not video_path:
            return video_path
        if not (self.nap_server_address and self.nap_server_port):
            return video_path
        if send_file is None:
            logger.debug("NapCat 文件转发模块不可用，直接返回本地路径")
            return video_path
        try:
            forwarded_path = await send_file(video_path, self.nap_server_address, self.nap_server_port)
            if forwarded_path:
                logger.info(f"NapCat file server returned video path: {forwarded_path}")
                return forwarded_path
            logger.warning("NapCat file server did not return a valid video path, falling back to local file")
        except Exception as e:
            logger.warning(f"NapCat file server transfer failed, falling back to local file: {e}")
        return video_path

    async def _cleanup_video_file(self, video_path: Optional[str]):
        """删除临时视频缓存（按照配置可选）"""
        if not video_path:
            return
        if self.save_video_enabled:
            return
        try:
            path = Path(video_path)
            if path.exists():
                path.unlink()
                logger.debug(f"已清理本地视频缓存: {path}")
        except Exception as e:
            logger.warning(f"清理视频文件失败: {e}")

    async def _create_video_component(self, video_path: Optional[str], video_url: Optional[str]):
        """根据配置构建最终 Video 组件，优先走 callback_api_base / NapCat / 远程 URL"""
        from astrbot.api.message_components import Video

        callback_api_base = self._get_callback_api_base()
        if callback_api_base and video_path:
            try:
                fs_component = Video.fromFileSystem(path=video_path)
                download_url = await fs_component.convert_to_web_link()  # type: ignore[attr-defined]
                if download_url:
                    logger.debug("已通过 callback_api_base 获取视频下载链接，使用 URL 发送")
                    return Video.fromURL(download_url)
            except Exception as e:
                logger.warning(f"callback_api_base 上传视频失败，改用其它方式: {e}")

        if video_path:
            final_video_path = await self._prepare_video_path(video_path)
            if final_video_path != video_path:
                return Video.fromFileSystem(path=final_video_path)

        if video_url:
            logger.debug("使用远程视频 URL 发送")
            return Video.fromURL(video_url)

        if video_path:
            return Video.fromFileSystem(path=video_path)

        raise ValueError("缺少可用的视频路径或链接")

    async def _generate_video_core(self, event: AstrMessageEvent, prompt: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """核心视频生成逻辑"""
        # 检查功能是否启用
        if not self.enabled:
            return None, None, "视频生成功能已禁用"
        
        # 提取图片
        images = await self._extract_images_from_message(event)
        if not images:
            return None, None, "未找到图片，请在消息中包含图片或引用包含图片的消息"
        
        # 使用第一张图片
        image_base64 = images[0]
        
        # 调用API生成视频
        video_url, error_msg = await self._call_grok_api(prompt, image_base64)
        if error_msg:
            return None, None, error_msg

        if not video_url:
            return None, None, "API未返回视频URL"

        local_path = await self._download_video(video_url)
        if not local_path:
            logger.warning("视频下载失败，改为直接使用远程 URL 发送")
            return video_url, None, None

        return video_url, local_path, None

    async def _async_generate_video(self, event: AstrMessageEvent, prompt: str):
        """异步视频生成，避免超时"""
        try:
            video_url, video_path, error_msg = await self._generate_video_core(event, prompt)
            
            if error_msg:
                await event.send(event.plain_result(f"❌ {error_msg}"))
                return
            
            if video_url or video_path:
                try:
                    video_component = await self._create_video_component(video_path, video_url)
                    await event.send(event.chain_result([video_component]))
                    if video_path:
                        await self._cleanup_video_file(video_path)
                except Exception as e:
                    logger.error(f"发送视频失败: {e}")
                    if video_path:
                        await event.send(event.plain_result(f"✅ 视频生成成功，但发送失败。文件已保存到: {video_path}"))
                    else:
                        await event.send(event.plain_result("✅ 视频生成成功，但发送失败。"))
            else:
                await event.send(event.plain_result("❌ 视频生成失败，请稍后再试"))
        
        except Exception as e:
            logger.error(f"异步视频生成异常: {e}")
            await event.send(event.plain_result(f"❌ 视频生成时遇到问题: {str(e)}"))

    @filter.llm_tool(name="generate_video_with_grok")
    async def llm_generate_video(self, event: AstrMessageEvent, prompt: str):
        """
        LLM函数调用工具：使用Grok根据图片和提示词生成视频。
        需要用户在消息中包含图片。

        Args:
            prompt(string): 视频生成提示词，描述想要生成的视频内容
        """
        try:
            # 群组访问检查
            access_error = self._check_group_access(event)
            if access_error:
                await event.send(event.plain_result(access_error))
                return
            
            # 检查是否包含图片
            images = await self._extract_images_from_message(event)
            if not images:
                await event.send(event.plain_result("❌ 视频生成需要您在消息中包含图片。请上传图片后再试。"))
                return
            
            # 立即发送状态消息
            await event.send(event.plain_result("🎬 正在使用Grok为您生成视频，请稍候..."))
            
            # 启动异步任务避免超时
            asyncio.create_task(self._async_generate_video(event, prompt))
        
        except Exception as e:
            logger.error(f"LLM视频生成工具异常: {e}")
            await event.send(event.plain_result(f"❌ 生成视频时遇到问题: {str(e)}"))

    @filter.command("视频")
    async def cmd_generate_video(self, event: AstrMessageEvent, *, prompt: str):
        """生成视频：/视频 <提示词>（需要包含图片）"""
        # 群组访问检查
        access_error = self._check_group_access(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        
        try:
            video_url, video_path, error_msg = await self._generate_video_core(event, prompt)
            
            if error_msg:
                yield event.plain_result(f"❌ {error_msg}")
                return
            
            if video_url or video_path:
                try:
                    video_component = await self._create_video_component(video_path, video_url)
                    yield event.chain_result([video_component])
                    if video_path:
                        await self._cleanup_video_file(video_path)
                except Exception as e:
                    logger.error(f"发送视频失败: {e}")
                    if video_path:
                        yield event.plain_result(f"✅ 视频生成成功，但发送失败。文件已保存到: {video_path}")
                    else:
                        yield event.plain_result("✅ 视频生成成功，但发送失败。")
            else:
                yield event.plain_result("❌ 视频生成失败，请稍后再试")
        
        except Exception as e:
            logger.error(f"视频生成命令异常: {e}")
            yield event.plain_result(f"❌ 生成视频时遇到问题: {str(e)}")

    @filter.command("grok测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """测试Grok API连接（管理员专用）"""
        if not self._is_admin(event):
            yield event.plain_result("此命令仅限管理员使用")
            return
        
        try:
            test_results = [Plain("🔍 Grok视频生成插件测试结果\n" + "="*30 + "\n\n")]
            
            # 检查配置
            if not self.api_key:
                test_results.append(Plain("❌ API密钥未配置\n"))
            else:
                test_results.append(Plain("✅ API密钥已配置\n"))
            
            test_results.append(Plain(f"📡 API地址: {self.api_url}\n"))
            test_results.append(Plain(f"🤖 模型ID: {self.model_id}\n"))
            test_results.append(Plain(f"⏱️ 超时时间: {self.timeout_seconds}秒\n"))
            test_results.append(Plain(f"🔄 最大重试: {self.max_retry_attempts}次\n"))
            test_results.append(Plain(f"📁 视频存储目录: {self.videos_dir}\n"))
            
            if self.enabled:
                test_results.append(Plain("✅ 功能已启用\n"))
            else:
                test_results.append(Plain("❌ 功能已禁用\n"))
            
            yield event.chain_result(test_results)
        
        except Exception as e:
            logger.error(f"测试命令异常: {e}")
            yield event.plain_result(f"❌ 测试失败: {str(e)}")

    @filter.command("grok帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """帮助信息"""
        help_text = (
            "🎬 Grok视频生成插件帮助\n\n"
            "使用方法：\n"
            "1. 发送一张图片\n"
            "2. 引用该图片发送：/视频 <提示词>\n\n"
            "示例：\n"
            "• /视频 让太阳升起来\n"
            "• /视频 添加下雨效果\n"
            "• /视频 让角色跳舞\n\n"
            "LLM函数调用：\n"
            "• generate_video_with_grok - AI可调用的视频生成工具\n\n"
            "管理员命令：\n"
            "• /grok测试 - 测试API连接\n"
            "• /grok帮助 - 显示此帮助信息\n\n"
            "注意：视频生成需要较长时间，请耐心等待"
        )
        yield event.plain_result(help_text)

    async def terminate(self):
        """插件卸载时调用"""
        logger.info("Grok视频生成插件已卸载")
