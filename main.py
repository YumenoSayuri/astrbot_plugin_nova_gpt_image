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
            self.session = aiohttp.ClientSession()
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

    def __init__(self, context: Context):
        super().__init__(context)
        self.config = context.get_config().get("astrbot_plugin_nova_gpt_image", {})
        
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

    async def extract_images_from_event(self, event: AstrMessageEvent) -> List[bytes]:
        img_bytes_list = []
        for seg in event.message_obj.message:
            if isinstance(seg, Reply) and seg.chain:
                for s_chain in seg.chain:
                    if isinstance(s_chain, Image):
                        if img := await self._image_component_to_bytes(s_chain):
                            img_bytes_list.append(img)
        for seg in event.message_obj.message:
            if isinstance(seg, Image):
                if img := await self._image_component_to_bytes(seg):
                    img_bytes_list.append(img)
        return img_bytes_list

    @filter.command("GPT生图", alias=["GPT"])
    async def gpt_draw_command(self, event: AstrMessageEvent, *, prompt: str = ""):
        '''调用 GPT 生图。用法: /GPT生图 [描述内容] (支持带图片进行图生图)'''
        if not prompt:
            # 尝试从 message_str 直接解析，兼容中间有空格的情况
            cmd = "/GPT生图"
            if "/GPT生图" in event.message_str:
                prompt = event.message_str.split("/GPT生图", 1)[1].strip()
            elif "/GPT" in event.message_str:
                prompt = event.message_str.split("/GPT", 1)[1].strip()
            
            # 清理 at 等信息
            prompt = re.sub(r'@\S+?\(\d+\)', '', prompt).strip()
            prompt = re.sub(r'@\S+', '', prompt).strip()

        if not prompt:
            yield event.plain_result("请告诉我你想画什么呀，辉宝主人~ (可以带图片哦)")
            return

        api_url, api_key, model = await self.get_api_config()
        if not api_url or not api_key:
            yield event.plain_result("未配置 API URL 或 Key，请在面板中设置提供商或手动填写哦~")
            return
            
        yield event.plain_result(f"Nova 正在为您生成「{prompt[:15]}...」，请稍候...")
        
        img_bytes_list = await self.extract_images_from_event(event)

        messages_content = []
        if img_bytes_list:
            for b in img_bytes_list[:3]:
                b64 = base64.b64encode(b).decode("utf-8")
                messages_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"}
                })
        
        messages_content.append({"type": "text", "text": f"画{prompt}"})
        
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": messages_content}],
            "stream": True
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        if not api_url.endswith("/chat/completions") and not api_url.endswith("/images/generations") and not api_url.endswith("/images/edits"):
            api_url = api_url.rstrip("/") + "/v1/chat/completions"

        try:
            async for result in self._stream_request(event, api_url, headers, payload):
                yield result
        except Exception as e:
            yield event.plain_result(f"画图出错啦：{str(e)}")

    async def _stream_request(self, event: AstrMessageEvent, url: str, headers: dict, payload: dict):
        buffer = ""
        image_pattern = re.compile(r'!\[.*?\]\((.*?)\)')
        yielded_urls = set()
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, headers=headers, json=payload, timeout=self.api_timeout) as response:
                    if response.status != 200:
                        text = await response.text()
                        yield event.plain_result(f"API 请求失败: HTTP {response.status}\n{text}")
                        return

                    async for line in response.content:
                        line = line.decode('utf-8').strip()
                        if not line: continue
                        if line == "data: [DONE]": break
                        
                        if line.startswith("data: "):
                            try:
                                data_json = json.loads(line[6:])
                                delta = data_json.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    buffer += content
                                    matches = image_pattern.findall(buffer)
                                    for img_url in matches:
                                        if img_url not in yielded_urls:
                                            yielded_urls.add(img_url)
                                            img_bytes = await self.downloader.download(img_url)
                                            if img_bytes:
                                                yield event.make_result().message(Image.fromBytes(img_bytes))
                                            else:
                                                yield event.plain_result(f"图片生成成功，但下载超时或失败惹...\n链接: {img_url}")
                            except json.JSONDecodeError:
                                pass
            except asyncio.TimeoutError:
                yield event.plain_result(f"API 请求超时啦，请在配置中调大超时时间（当前 {self.api_timeout} 秒）")

    async def terminate(self):
        await self.downloader.close()
