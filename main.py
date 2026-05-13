import asyncio
import aiohttp
import json
import re
import base64
from typing import List

from astrbot import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Reply
from astrbot.core.provider import Provider

@register("astrbot_plugin_nova_gpt_image", "Nova", "支持模型上游非流式和流式生图，支持图生图", "2.0.0", "https://github.com/YumenoSayuri/astrbot_plugin_nova_gpt_image")
class NovaGptImagePlugin(Star):
    class ImageDownloader:
        def __init__(self, timeout: int = 60, retries: int = 3):
            # 将缓冲区提升到 100MB 应对超大 4K 图片
            self.session = aiohttp.ClientSession(read_bufsize=1024 * 1024 * 100)
            self.timeout = timeout
            self.retries = retries

        async def download(self, url: str) -> bytes | None:
            last_error = None
            for attempt in range(self.retries):
                try:
                    if attempt > 0:
                        await asyncio.sleep(1 * attempt)
                    async with self.session.get(url, timeout=self.timeout) as resp:
                        if resp.status == 200:
                            return await resp.read()
                except Exception as e:
                    last_error = str(e)
            logger.warning(f"[NovaGptImage] 图片下载失败: {url}, 错误: {last_error}")
            return None

        async def close(self):
            if not self.session.closed:
                await self.session.close()

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if config is not None else context.get_config().get("astrbot_plugin_nova_gpt_image", {})
        
        self.provider_id = self.config.get("provider_id", "")
        self.manual_api_url = self.config.get("api_url", "")
        self.manual_api_key = self.config.get("api_key", "")
        self.model_name = self.config.get("model", "free-gpt-5.4-all")
        
        dl_timeout = self.config.get("download_timeout", 60)
        dl_retries = self.config.get("download_retries", 3)
        self.api_timeout = self.config.get("api_timeout", 120)
        
        # 解析白名单 QQ 号
        whitelist_str = self.config.get("whitelist_qq", "")
        self.whitelist_qq = set()
        if whitelist_str:
            for qq in whitelist_str.split(","):
                qq = qq.strip()
                if qq:
                    self.whitelist_qq.add(qq)
        
        self.downloader = self.ImageDownloader(timeout=dl_timeout, retries=dl_retries)

    async def get_api_config(self) -> tuple[str, str, str]:
        if self.provider_id:
            provider = self.context.get_provider_by_id(self.provider_id)
            if provider:
                try:
                    conf = provider.provider_config
                    url = conf.get("api_base", "")
                    keys = provider.get_keys()
                    key = keys[0] if keys else ""
                    model = provider.get_model()
                    if url and key:
                        return url, key, model
                except Exception as e:
                    logger.warning(f"[NovaGptImage] 从提供商获取配置失败: {e}")
        return self.manual_api_url, self.manual_api_key, self.model_name

    async def select_route_config(self, is_whitelist: bool, has_images: bool) -> tuple[str, str, str]:
        """
        按四路策略选择配置：
        1. 白名单图生图
        2. 白名单文生图
        3. 普通用户图生图
        4. 普通用户文生图
        任一路缺失时回退到旧 provider/manual 单路配置
        """
        route_url = ""
        route_model = ""

        if is_whitelist and has_images:
            route_url = self.config.get("whitelist_i2i_api_url", "").strip()
            route_model = self.config.get("whitelist_i2i_model", "").strip()
        elif is_whitelist and not has_images:
            route_url = self.config.get("whitelist_t2i_api_url", "").strip()
            route_model = self.config.get("whitelist_t2i_model", "").strip()
        elif (not is_whitelist) and has_images:
            route_url = self.config.get("normal_i2i_api_url", "").strip()
            route_model = self.config.get("normal_i2i_model", "").strip()
        else:
            route_url = self.config.get("normal_t2i_api_url", "").strip()
            route_model = self.config.get("normal_t2i_model", "").strip()

        fallback_url, fallback_key, fallback_model = await self.get_api_config()
        api_url = route_url or fallback_url
        api_key = self.manual_api_key or fallback_key
        model = route_model or fallback_model

        route_name = (
            "白名单图生图" if is_whitelist and has_images else
            "白名单文生图" if is_whitelist and not has_images else
            "普通图生图" if (not is_whitelist) and has_images else
            "普通文生图"
        )
        logger.info(f"[NovaGptImage] 路由选择: {route_name} | url={api_url} | model={model}")
        return api_url, api_key, model

    async def _image_component_to_bytes(self, image_comp: Image) -> bytes | None:
        if hasattr(image_comp, "convert_to_base64"):
            try:
                base64_str = await image_comp.convert_to_base64()
                if base64_str:
                    if base64_str.startswith("data:image/"):
                        base64_str = base64_str.split(",", 1)[1]
                    return base64.b64decode(base64_str)
            except Exception as e:
                logger.warning(f"获取图片 base64 失败: {e}")
        if image_comp.url:
            return await self.downloader.download(image_comp.url)
        return None

    def parse_size_from_prompt(self, prompt: str, is_whitelist: bool) -> str | None:
        """
        从 prompt 中智能解析尺寸参数
        返回: 具体尺寸字符串（如 "3840x2160"）或 None（表示不传 size）
        """
        if not is_whitelist:
            return None  # 非白名单用户不传 size
        
        prompt_lower = prompt.lower()
        
        # 提取质量关键词
        quality = None
        if "4k" in prompt_lower:
            quality = "4k"
        elif "2k" in prompt_lower:
            quality = "2k"
        elif "1k" in prompt_lower or "1080" in prompt_lower:
            quality = "1k"
        
        # 提取比例关键词（支持多种写法）
        ratio = None
        # 匹配 1:1, 1：1, 1-1, 1比1 等
        if re.search(r'1[:\：\-比]1', prompt):
            ratio = "1:1"
        elif re.search(r'16[:\：\-比]9', prompt):
            ratio = "16:9"
        elif re.search(r'9[:\：\-比]16', prompt):
            ratio = "9:16"
        elif re.search(r'4[:\：\-比]3', prompt):
            ratio = "4:3"
        elif re.search(r'3[:\：\-比]4', prompt):
            ratio = "3:4"
        elif re.search(r'3[:\：\-比]2', prompt):
            ratio = "3:2"
        elif re.search(r'2[:\：\-比]3', prompt):
            ratio = "2:3"
        
        # 只有同时有质量和比例才返回具体尺寸
        if quality and ratio:
            size_map = {
                ("4k", "16:9"): "3840x2160",
                ("4k", "9:16"): "2160x3840",
                ("4k", "1:1"): "2880x2880",
                ("4k", "4:3"): "2560x1920",  # 约 4.9M 像素
                ("4k", "3:4"): "1920x2560",
                ("4k", "3:2"): "2880x1920",  # 约 5.5M 像素
                ("4k", "2:3"): "1920x2880",
                ("2k", "16:9"): "2560x1440",
                ("2k", "9:16"): "1440x2560",
                ("2k", "1:1"): "2048x2048",
                ("2k", "4:3"): "1920x1440",
                ("2k", "3:4"): "1440x1920",
                ("2k", "3:2"): "2048x1365",  # 约 2.8M 像素
                ("2k", "2:3"): "1365x2048",
                ("1k", "16:9"): "1920x1080",
                ("1k", "9:16"): "1080x1920",
                ("1k", "1:1"): "1024x1024",
                ("1k", "4:3"): "1280x960",
                ("1k", "3:4"): "960x1280",
                ("1k", "3:2"): "1440x960",
                ("1k", "2:3"): "960x1440",
            }
            size = size_map.get((quality, ratio))
            if size:
                logger.info(f"[NovaGptImage] 智能识别尺寸: {quality} + {ratio} → {size}")
                return size
        
        # 只有其中一个或都没有，不传 size
        return None

    async def extract_images_from_event(self, event: AstrMessageEvent) -> tuple[List[bytes], List[str]]:
        img_bytes_list = []
        img_urls_list = []
        
        async def process_image_seg(img_seg: Image):
            # 提取 bytes，用于兜底 chat/completions base64
            if img := await self._image_component_to_bytes(img_seg):
                img_bytes_list.append(img)
            # 提取原始 url，用于 generations 规避 chunk too big
            if img_seg.url:
                img_urls_list.append(img_seg.url)

        for seg in event.message_obj.message:
            if isinstance(seg, Reply) and seg.chain:
                for s_chain in seg.chain:
                    if isinstance(s_chain, Image):
                        await process_image_seg(s_chain)
        for seg in event.message_obj.message:
            if isinstance(seg, Image):
                await process_image_seg(seg)
                
        return img_bytes_list, img_urls_list

    @filter.command("GPT生图", alias=["GPT"])
    async def gpt_draw_command(self, event: AstrMessageEvent, prompt: str = ""):
        # 直接使用底层字符串分割，完美保留第一个空格后的所有空格与内容
        arg = event.message_str.partition(" ")[2].strip()
        
        # 兜底：如果用户连空格都没打，比如直接回复了 /GPT生图画个猫
        if not arg:
            msg_str = event.message_str
            if "/GPT生图" in msg_str:
                arg = msg_str.split("/GPT生图", 1)[1].strip()
            elif "/GPT" in msg_str:
                arg = msg_str.split("/GPT", 1)[1].strip()
            elif "GPT生图" in msg_str:
                arg = msg_str.split("GPT生图", 1)[1].strip()
            elif "GPT" in msg_str:
                arg = msg_str.split("GPT", 1)[1].strip()
                
        prompt = arg
        
        # 清除任何残留的 @ 文本
        prompt = re.sub(r'@\S+?\(\d+\)', '', prompt).strip()
        prompt = re.sub(r'@\S+', '', prompt).strip()

        if not prompt:
            yield event.plain_result("请告诉我你想画什么呀，辉宝主人~ (可以带图片哦)")
            return

        img_bytes_list, img_urls_list = await self.extract_images_from_event(event)
        user_qq = event.get_sender_id()
        is_whitelist = user_qq in self.whitelist_qq
        has_images = bool(img_bytes_list or img_urls_list)

        api_url, api_key, model = await self.select_route_config(is_whitelist, has_images)
        if not api_url or not api_key:
            yield event.plain_result("未配置 API URL 或 Key，请在面板中设置对应路由、提供商或手动填写哦~")
            return
            
        # 移除任何模型提供商带的中文前缀（如 "鸢-"），如果指定的话。并优化文案。
        display_model = re.sub(r'^[\u4e00-\u9fa5]+-', '', model)
        route_tip = "图生图" if has_images else "文生图"
        yield event.plain_result(f"正在用 {display_model} ({route_tip}) 为您生成「{prompt[:15]}...」，请稍候...")

        url_lower = api_url.lower()
        
        # 严格尊崇用户填写的端点，不做越权拦截
        is_images_api = False
        if "images/generations" in url_lower or "images/edits" in url_lower or "images/" in url_lower:
            is_images_api = True
            logger.info(f"[NovaGptImage] 匹配到 Image API 端点: {api_url}")
        elif not url_lower.endswith("/chat/completions"):
            # 仅在非 images 且未写明 chat/completions 时补全
            api_url = api_url.rstrip("/") + "/v1/chat/completions"
            logger.info(f"[NovaGptImage] 补全 Chat API 端点: {api_url}")
            
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        if is_images_api:
            # 适配 images 体系；若配置成 edits 也复用同一套请求发送
            # 智能解析尺寸（仅对白名单生效）
            size = self.parse_size_from_prompt(prompt, is_whitelist)
            
            payload = {
                "model": model,
                "prompt": prompt,
                "n": 1,
                "output_format": "png",
                "quality": "high"
            }
            
            # 只有白名单用户且识别到完整的质量+比例才传 size
            if size:
                payload["size"] = size
                logger.info(f"[NovaGptImage] 白名单用户 {user_qq}，使用尺寸: {size}")
            else:
                if is_whitelist:
                    logger.info(f"[NovaGptImage] 白名单用户 {user_qq}，但未识别到完整参数，使用默认尺寸")
                else:
                    logger.info(f"[NovaGptImage] 非白名单用户 {user_qq}，使用默认尺寸")
            
            # 为了防止 Base64 造成 Chunk too big，优先直接传 URL，如果没 URL 才回退 Base64
            if img_urls_list:
                payload["image"] = img_urls_list[:3]
                logger.info("[NovaGptImage] Image API 图生图模式，直接注入 URL 数组防过载")
            elif img_bytes_list:
                b64_urls = []
                for b in img_bytes_list[:3]:
                    b64 = base64.b64encode(b).decode("utf-8")
                    b64_urls.append(f"data:image/png;base64,{b64}")
                payload["image"] = b64_urls
                logger.info("[NovaGptImage] Image API 图生图模式，注入 Base64 图像数组兜底")
                
            try:
                logger.info(f"[NovaGptImage] Calling Image API: {api_url}")
                async for result in self._non_stream_request(event, api_url, headers, payload):
                    yield result
            except Exception as e:
                yield event.plain_result(f"画图出错啦：{str(e)}")
        else:
            # 走 chat/completions 体系 (多模态通用)
            messages_content = []
            if img_bytes_list:
                for b in img_bytes_list[:3]:
                    b64 = base64.b64encode(b).decode("utf-8")
                    # 使用标准的前缀来让大部分视觉模型识别
                    messages_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}
                    })
            messages_content.append({"type": "text", "text": f"画{prompt}"})
            
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": messages_content}],
                "stream": True # 默认开启流式，如果上游假流式，后续会自动兜底提取 json
            }
            
            try:
                logger.info(f"[NovaGptImage] Calling Chat API: {api_url}")
                async for result in self._stream_request(event, api_url, headers, payload):
                    yield result
            except Exception as e:
                yield event.plain_result(f"画图出错啦：{str(e)}")


    async def _non_stream_request(self, event: AstrMessageEvent, url: str, headers: dict, payload: dict):
        """专门处理直接返回 JSON 结果的图像接口 (如 v1/images/generations)"""
        # 移除读取内容的大小限制，应对 4k 图片等返回超长 Base64 导致的 Chunk too big 问题
        async with aiohttp.ClientSession(read_bufsize=1024 * 1024 * 100) as session:
            try:
                async with session.post(url, headers=headers, json=payload, timeout=self.api_timeout) as response:
                    if response.status != 200:
                        text = await response.text()
                        yield event.plain_result(f"API 请求失败: HTTP {response.status}\n{text}")
                        return
                    
                    data = await response.json()
                    
                    # 尝试解析 openai 风格的 data[0].url
                    if isinstance(data.get("data"), list) and len(data["data"]) > 0:
                        img_url = data["data"][0].get("url")
                        if img_url:
                            img_bytes = await self.downloader.download(img_url)
                            if img_bytes:
                                yield event.chain_result([Image.fromBytes(img_bytes)])
                                return
                            else:
                                yield event.plain_result(f"生成成功，但下载失败惹...\n链接: {img_url}")
                                return
                                
                    # 尝试解析 openai 风格的 data[0].b64_json
                    if isinstance(data.get("data"), list) and len(data["data"]) > 0:
                        b64_json = data["data"][0].get("b64_json")
                        if b64_json:
                            img_bytes = base64.b64decode(b64_json)
                            yield event.chain_result([Image.fromBytes(img_bytes)])
                            return

                    yield event.plain_result(f"未能从响应中提取图片，返回内容: {str(data)[:200]}")
            except asyncio.TimeoutError:
                yield event.plain_result(f"API 请求超时啦，请在配置中调大超时时间（当前 {self.api_timeout} 秒）")

    async def _stream_request(self, event: AstrMessageEvent, url: str, headers: dict, payload: dict):
        """处理流式和可能的伪流式 chat/completions"""
        buffer = ""
        image_pattern = re.compile(r'!\[.*?\]\((.*?)\)')
        yielded_urls = set()
        
        # 对于流式请求，由于 Base64 图片可能在一行内返回，超过默认的 Chunk 大小限制，因此提升缓冲区到 100MB。
        async with aiohttp.ClientSession(auto_decompress=True, read_bufsize=1024 * 1024 * 100) as session:
            try:
                async with session.post(url, headers=headers, json=payload, timeout=self.api_timeout) as response:
                    if response.status != 200:
                        text = await response.text()
                        yield event.plain_result(f"API 请求失败: HTTP {response.status}\n{text}")
                        return

                    # 检查是否是真的流式响应
                    content_type = response.headers.get("Content-Type", "")
                    if "text/event-stream" not in content_type:
                         # 有些接口就算传了 stream=True 也会一次性返回 json
                         data = await response.json()
                         choices = data.get("choices", [])
                         content = ""
                         if choices and len(choices) > 0:
                             content = choices[0].get("message", {}).get("content", "")
                         
                         if content:
                             matches = image_pattern.findall(content)
                             if matches:
                                 for img_url in matches:
                                     if img_url.startswith("data:image/"):
                                         try:
                                             b64_data = img_url.split(",", 1)[1]
                                             img_bytes = base64.b64decode(b64_data)
                                             yield event.chain_result([Image.fromBytes(img_bytes)])
                                         except Exception as e:
                                             yield event.plain_result(f"Base64图片解析失败: {e}")
                                     else:
                                         img_bytes = await self.downloader.download(img_url)
                                         if img_bytes: yield event.chain_result([Image.fromBytes(img_bytes)])
                                         else: yield event.plain_result(f"下载失败: {img_url}")
                                 return
                         yield event.plain_result(f"非流式返回中未找到图片惹...\n详情：{str(data)[:100]}")
                         return

                    # 真正的流式处理，由于已经提升了 read_bufsize，这里可以直接按行读取
                    async for chunk in response.content:
                        line = chunk.decode('utf-8').strip()
                        if not line: continue
                        if line == "data: [DONE]": break
                        
                        if line.startswith("data: "):
                            try:
                                data_json = json.loads(line[6:])
                                choices = data_json.get("choices", [])
                                if choices and len(choices) > 0:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        buffer += content
                                    matches = image_pattern.findall(buffer)
                                    for img_url in matches:
                                        if img_url not in yielded_urls:
                                            yielded_urls.add(img_url)
                                            logger.info(f"[NovaGptImage] 检测到图片链接: {img_url[:100]}")
                                            if img_url.startswith("data:image/"):
                                                try:
                                                    b64_data = img_url.split(",", 1)[1]
                                                    img_bytes = base64.b64decode(b64_data)
                                                    yield event.chain_result([Image.fromBytes(img_bytes)])
                                                    logger.info("[NovaGptImage] Base64 图片发送成功")
                                                except Exception as e:
                                                    logger.error(f"[NovaGptImage] Base64 解析失败: {e}")
                                                    yield event.plain_result(f"Base64 图片解析失败: {e}")
                                            else:
                                                logger.info(f"[NovaGptImage] 开始下载图片: {img_url}")
                                                img_bytes = await self.downloader.download(img_url)
                                                if img_bytes:
                                                    logger.info(f"[NovaGptImage] 图片下载成功，大小: {len(img_bytes)} bytes")
                                                    yield event.chain_result([Image.fromBytes(img_bytes)])
                                                else:
                                                    logger.warning(f"[NovaGptImage] 图片下载失败: {img_url}")
                                                    yield event.plain_result(f"图片生成成功，但下载超时或失败惹...\n链接: {img_url}")
                            except json.JSONDecodeError:
                                pass
            except asyncio.TimeoutError:
                yield event.plain_result(f"API 请求超时啦，请在配置中调大超时时间（当前 {self.api_timeout} 秒）")

    async def terminate(self):
        await self.downloader.close()
