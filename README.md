# astrbot_plugin_nova_gpt_image

支持基于 GPT-5.4 及其它兼容 OpenAI 流式图片输出格式的大模型生图专属插件。

✨ **由全能战略官人格 Agent Nova 专属定制开发** ✨

仓库地址：[https://github.com/YumenoSayuri/astrbot_plugin_nova_gpt_image](https://github.com/YumenoSayuri/astrbot_plugin_nova_gpt_image)

## 特性

- **支持流式（Streaming）与非流式**：采用多线程异步流式截获 API 吐出的 Markdown 格式图片，第一时间推送。
- **支持多模态图生图**：在对话中附带图片，或回复（引用）带图片的聊天记录，输入触发词即可自动转换为图生图模式！
- **完备的下载机制**：支持自定义超时时间、下载重试次数，确保生图成功后不再因为 CDN 慢而失败。
- **自定义配置**：支持在 AstrBot 控制面板选择全局的 Provider（例如您自建的模型代理），或者在插件面板中手动配置 API URL 和 API Key。
- **指令兼容**：可直接使用 `/GPT生图 [描述]` 或 `/GPT [描述]` 进行触发，自动识别图生图/文生图需求。

## 配置说明

在控制台的插件配置面板，找到 `astrbot_plugin_nova_gpt_image`：
1. **API提供商 (优先)**：如果您已在 AstrBot 配置了 Provider，可以直接选择它，插件会自动拉取其 URL 和 Key。
2. **手动配置 API URL / Key**：如果不使用 Provider，直接填入您的中转接口或直连接口。
3. **超时与重试**：根据您所使用上游 API 的网络状态，适度调整“图片下载超时时间”与“API请求等待超时”。

## 声明

仅供个人学习交流使用。