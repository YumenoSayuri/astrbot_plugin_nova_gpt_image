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

        api_url, api_key, model = await self.get_api_config()
        if not api_url or not api_key:
            yield event.plain_result("未配置 API URL 或 Key，请在面板中设置提供商或手动填写哦~")
            return
            
        # 移除任何模型提供商带的中文前缀（如 "鸢-"），如果指定的话。并优化文案。
        display_model = re.sub(r'^[\u4e00-\u9fa5]+-', '', model)
        yield event.plain_result(f"正在用 {display_model} 为您生成「{prompt[:15]}...」，请稍候...")
        
        img_bytes_list, img_urls_list = await self.extract_images_from_event(event)

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
            # 适配 v1/images/generations 体系 (支持内嵌 image 数组的图生图)
            payload = {
                "model": model,
                "prompt": prompt,
                "n": 1,
                "size": "1024x1024"
            }
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
                                            if img_url.startswith("data:image/"):
                                                try:
                                                    b64_data = img_url.split(",", 1)[1]
                                                    img_bytes = base64.b64decode(b64_data)
                                                    yield event.chain_result([Image.fromBytes(img_bytes)])
                                                except Exception as e:
                                                    yield event.plain_result(f"Base64 图片解析失败: {e}")
                                            else:
                                                img_bytes = await self.downloader.download(img_url)
                                                if img_bytes:
                                                    yield event.chain_result([Image.fromBytes(img_bytes)])
                                                else:
                                                    yield event.plain_result(f"图片生成成功，但下载超时或失败惹...\n链接: {img_url}")
                            except json.JSONDecodeError:
                                pass
            except asyncio.TimeoutError:
                yield event.plain_result(f"API 请求超时啦，请在配置中调大超时时间（当前 {self.api_timeout} 秒）")

    async def terminate(self):
        await self.downloader.close()
