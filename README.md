# WEB Search — AI 驱动的多语言多平台搜索系统

一个基于 FastAPI + 流式 SSE 的本地 Web 搜索工具，支持关键词多语言翻译、并发多平台搜索、双语结果展示和 AI 综合分析。
Demo : http://107.173.49.201/

---

## 功能特性

### 搜索能力
| 平台 | 搜索方式 | 备注 |
|------|----------|------|
| **Google** | Google Custom Search API | 无 Key 时自动回退 DuckDuckGo |
| **YouTube** | Google CSE `site:youtube.com` | 返回带缩略图的视频卡片 |
| **arXiv** | 官方 Atom API（`ti: OR abs:`） | 学术论文，含作者/日期 |
| **Reddit** | Reddit JSON API | 过滤低质量帖子（score < 2） |
| **ResearchGate** | Google CSE `site:researchgate.net/publication` | 仅匹配论文页面 |
| **DEF CON** | Google CSE `site:defcon.org` | 安全会议演讲 |
| **Black Hat** | Google CSE `site:blackhat.com` | 安全研究简报 |
| **CCC** | Google CSE `site:media.ccc.de OR site:ccc.de` | Chaos Computer Club 演讲存档 |

### 翻译与语言
- 输入任意语言关键词，自动翻译为英/法/韩/日/德/西/俄语（可选）
- 点击任意翻译词条，以该语言重新搜索，获取目标语言的原文结果
- 搜索结果标题和摘要自动翻译为中文，支持 **原文 / 双语 / 仅中文** 三档切换

### AI 综合分析
- 接入 Anthropic API，对全平台搜索结果生成 4-6 句中文综合摘要
- 支持自定义 **Base URL**（中转站/代理）和 **Model ID**，兼容任意 OpenAI 兼容接口

### 界面
- Genspark 风格暗色主界面，7 套配色主题（暗夜 / 海洋 / 森林 / 暮色 / 玫瑰 / Nord / 浅色）
- 流式加载：各平台结果逐个出现，含骨架屏占位
- 平台标签页过滤、一键导出 Markdown

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动服务

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

浏览器访问 **http://localhost:8000**

### 3. 配置 API（可选）

点击页面右上角「设置」，填入以下配置（保存在浏览器 localStorage，不上传服务器）：

| 配置项 | 说明 |
|--------|------|
| Google API Key | [Google Cloud Console](https://console.cloud.google.com) 获取 |
| Search Engine ID (cx) | [Programmable Search Engine](https://programmablesearchengine.google.com) 创建，勾选「搜索整个网络」 |
| Anthropic API Key | [console.anthropic.com](https://console.anthropic.com) 获取 |
| API Base URL | 中转站地址，如 `https://your-proxy.com/v1`，留空使用官方 |
| Model ID | 自定义模型，如 `claude-opus-4-5`，留空使用默认 |

也可通过环境变量在启动时配置：

```bash
GOOGLE_API_KEY=AIza... \
GOOGLE_CX=xxxxxxxx \
ANTHROPIC_API_KEY=sk-ant-... \
uvicorn app:app --port 8000
```

---

## 项目结构

```
WEB_Search/
├── app.py          # FastAPI 后端：翻译、搜索、AI 摘要、SSE 流式接口
├── requirements.txt
└── static/
    └── index.html  # 前端单页应用
```

### 主要 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `GET /` | GET | 前端页面 |
| `POST /search-stream` | POST | SSE 流式搜索主接口 |
| `POST /api/test-google` | POST | 测试 Google API 凭证是否有效 |
| `GET /status` | GET | 返回服务端 API 配置状态 |

### `/search-stream` 请求体

```json
{
  "keyword": "网络安全",
  "languages": ["en", "fr", "ko"],
  "platforms": ["duckduckgo", "youtube", "arxiv", "reddit", "researchgate", "defcon", "blackhat", "ccc"],
  "google_api_key": "AIza...",
  "google_cx": "xxxxxxxx",
  "api_key": "sk-ant-...",
  "anthropic_base_url": "https://your-proxy.com/v1",
  "anthropic_model": "claude-haiku-4-5-20251001",
  "query_override": "cybersecurity"
}
```

### SSE 事件类型

| `type` | 内容 |
|--------|------|
| `translations` | 各语言翻译结果数组 |
| `query` | 实际使用的搜索词 |
| `platform` | 单个平台的搜索结果 |
| `summary` | AI 综合分析文本 |

---

## 依赖

| 包 | 用途 |
|----|------|
| `fastapi` + `uvicorn` | Web 框架与服务器 |
| `httpx` | 异步 HTTP 客户端 |
| `deep-translator` | 关键词翻译（Google Translate） |
| `feedparser` | 解析 arXiv Atom feed |
| `ddgs` | DuckDuckGo 搜索（无 Google Key 时回退） |
| `anthropic` | AI 摘要（可选） |
