# 政务知识问答（RAG）— 教学主线 + 政务试点

一套基于 **LangChain** 的 RAG 实战项目：既保留单文件教学主线（`main.py`），也提供可运行的
**权限感知 + 审计**政务问答系统（`app/` + `frontend/`）。

后端 **FastAPI + SSE**，前端 **React 18 + TypeScript + Vite**（开发期 Vite 代理，生产期由后端同源托管），
同时保留一份**零依赖单页**（`app/static/index.html`）作为离线/回退界面。

> 完整链路：文档加载 → 密级/部门裁剪 → FAISS + BM25 混合检索 → RRF 融合 → `[n]` 引用编号 → LLM 流式回答 → 审计留痕

## 🏛️ 政务试点（可运行）

- 方案设计：[docs/政府级RAG改造方案.md](docs/政府级RAG改造方案.md)（现状差距、目标架构、密级治理、拒答策略、分期路线）
- 可运行原型：[app/README.md](app/README.md)（FastAPI + SSE + 文档级 ACL + 审计 + React 前端）
- 技术深改：[docs/生产级RAG架构演进分析.md](docs/生产级RAG架构演进分析.md)（Rerank、缓存、可观测、多租户、容器化）

```bash
# 后端（需 Ollama 已运行）
# 首次先复制 .env.example 为 .env，并设置 DEMO_PASSWORD 与 SESSION_SECRET
uv run uvicorn app.server:app --host 127.0.0.1 --port 8000

# 前端（开发模式，可选：不启动则用内置单页）
cd frontend && npm install && npm run dev
```

打开 `http://127.0.0.1:5173`（Vite 开发服务器）或 `http://127.0.0.1:8000`（后端托管），
输入演示口令后，用不同账号对比可见范围。

核心原则：**越权内容不得进入生成上下文**——先按用户裁剪候选集，再检索，最后生成；没有依据直接拒答，不调用 LLM。

## 🎯 项目目标

- 理解 RAG（检索增强生成）的工作原理
- 掌握 LangChain 的核心概念（Document、Embeddings、VectorStore）
- 跑通个人知识库问答系统
- 了解向量索引、相似度检索等关键概念
- 了解政务场景下的权限裁剪、引用可核查与审计留痕

## 🛠️ 技术栈

**后端 / 检索**

- **Python 3.11+**、**FastAPI + Uvicorn**：HTTP 服务与 SSE 流式接口
- **LangChain 1.x**（核心组件 + LCEL）、**langchain-ollama**、**langchain-deepseek**（可选在线 LLM）
- **Ollama**：本地 LLM（默认 `deepseek-r1:1.5b`，推荐 `qwen3:4b`）与 Embedding（默认 `bge-m3`）
- **FAISS**：稠密向量检索；**BM25（rank-bm25）+ jieba**：中文稀疏检索；**RRF**：多路结果融合
- **itsdangerous**：签名 Cookie 会话；**python-dotenv**：环境变量；**PyYAML**：语料 front matter

**前端**

- **React 18 + TypeScript 5.5 + Vite 5**：组件化 SPA，开发期 `/api` 代理到后端
- **零依赖单页**（`app/static/index.html`）：不构建、不联网也能运行，作为回退界面
- **自建 Markdown 渲染器**：不用 `innerHTML`，加粗/列表/表格/代码块/`[n]` 引用角标

**质量 / 评测**

- **pytest**：171 个用例（权限矩阵、会话、审计、检索、预算、评测台）
- **evaluation/**：Recall@K、MRR、证据命中率、拒答准确率、越权泄露数、时延分位

## 🧭 前后端选型与部署形态

| 层 | 选型 | 说明 |
|---|---|---|
| 前端 | React 18 + TypeScript + Vite 5 | 组件化、类型约束；Vite 开发代理解决 Cookie 同源问题 |
| 回退界面 | 原生 HTML/CSS/JS 单页 | 零依赖、断网可用，不构建也能演示 |
| 后端 | FastAPI + Uvicorn | 同步端点 + `StreamingResponse`（SSE），接口统一 `/api/*` |
| 会话 | itsdangerous 签名 Cookie | HttpOnly / SameSite=Lax；服务端校验过期 + `jti` 吊销，无 Redis |
| 检索 | FAISS + BM25 + RRF | 单进程内存索引；语料变化按指纹重建 |
| 模型 | Ollama（默认）/ DeepSeek（可选） | Embedding 始终本地，切 LLM 不重建索引 |
| 评测 | pytest + `evaluation/` | 离线校验进 CI；真实指标需本地模型 |
| 交付 | 单机 / 内网 | 无数据库、无消息队列；生产建议 Nginx + HTTPS + 只读语料目录 |

**同源策略**：开发期 Vite 把 `/api` 代理到 `127.0.0.1:8000`，浏览器视角同源，Cookie 正常回传；
生产期 `npm run build` 后由 FastAPI 托管 `frontend/dist`，仍然同源，不需要 CORS。

**`/` 的解析顺序**：`frontend/dist/index.html` 存在 → React 构建产物；否则回退 `app/static/index.html`。
构建产物变化后需要重启后端（资源挂载在启动时完成）。

## 📦 安装

```bash
# 1. 克隆项目
git clone https://github.com/lanyangyanglazy/KNOWRAG-10.git
cd KNOWRAG-10

# 2. 同步依赖（推荐使用 uv）
uv sync

# 3. 确保 Ollama 运行中
ollama serve

# 4. 下载模型（与 .env.example 默认组合一致）
ollama pull qwen3:4b      # 生成；也可用 deepseek-r1:1.5b（质量较弱）
ollama pull bge-m3        # 嵌入；拒答阈值按 bge-m3 校准，换模型需重新校准

# 5. 配置环境变量（口令、会话密钥、模型 provider 等）
cp .env.example .env

# 6. 前端依赖（可选，仅 React 界面需要）
cd frontend && npm install && cd ..
```

> **注意**：项目使用 `uv` 管理依赖，确保已安装 uv。如果未安装，请运行：`pip install uv`

## 🚀 运行

### 教学主线（CLI）

```bash
uv run python main.py
```

首次运行从 `data/knowledge_base.txt` 构建索引到 `faiss_index/`，之后直接加载。

### 政务问答（后端 + 前端）

```bash
# 后端（端口 8000；首次请求会加载语料并构建 web_index/）
uv run uvicorn app.server:app --host 127.0.0.1 --port 8000

# 前端开发模式（端口 5173，/api 代理到 8000）
cd frontend
npm run dev

# 前端生产构建（产物 frontend/dist，由后端同源托管）
npm run build
```

| 入口 | 地址 | 说明 |
|---|---|---|
| Vite 开发服务器 | http://127.0.0.1:5173 | 热更新，`/api` 代理到后端 |
| 后端托管 | http://127.0.0.1:8000 | 有 `frontend/dist` 用 React，否则回退单页 |

⚠️ 请使用 `uv run` 或先激活虚拟环境（Windows：`.venv\Scripts\activate`），
否则会缺依赖（如 `ModuleNotFoundError: No module named 'langchain_ollama'`）。
生产构建后需重启后端，`/assets` 挂载在启动时完成。

## 🌐 使用 DeepSeek 在线模型（可选）

本项目默认使用本地 Ollama（零成本、离线）。你也可以切换到**在线 DeepSeek**——价格极低（`deepseek-chat` 约 $0.14/百万输入 token）、质量更高（DeepSeek-V3.1），适合没有本地 GPU 或想要更好生成质量的场景。

### 本地 vs 在线

| 维度 | 本地 Ollama | 在线 DeepSeek |
|---|---|---|
| 成本 | 免费 | 极便宜（约 $0.14/1M 输入 token） |
| 质量 | r1:1.5b 较弱 | V3.1，接近 GPT-4 水平 |
| 硬件 | 需本地内存/显存 | 无要求 |
| 网络 | 离线 | 需联网 |
| 隐私 | 数据不出本机 | 数据上云 |

### 配置步骤

1. **获取 API Key**：访问 [DeepSeek 开放平台](https://platform.deepseek.com/api_keys) 创建并充值（[定价说明](https://api-docs.deepseek.com/quick_start/pricing)）。
2. **复制配置模板**：`cp .env.example .env`
3. **编辑 `.env`**，填入：
   ```env
   DEEPSEEK_API_KEY=sk-你的真实key
   LLM_PROVIDER=deepseek
   ```
4. **运行**（命令不变）：
   ```bash
   uv run python main.py
   ```

### 💡 学习点：在线 LLM + 本地 Embedding 的混合架构

DeepSeek **不提供 Embedding API**，因此本项目在在线模式下依然用本地 Ollama（`nomic-embed-text`）做嵌入，只有"生成答案"这一步走 DeepSeek。这演示了一个重要模式：

- **LLM 与 Embedding 解耦**：两者各取所长——LLM 用在线的高质量模型，Embedding 用本地零成本模型。
- **索引可复用**：因为 Embedding 模型不变，切换 LLM provider 时 `faiss_index/` 无需重建，可平滑迁移。
- **代码无感知**：`ChatOllama` 与 `ChatDeepSeek` 都实现 LangChain 的 `ChatModel` 接口，下游 `llm.invoke(prompt).content` 完全一致。

> 想看推理过程？在 `.env` 设置 `DEEPSEEK_MODEL=deepseek-reasoner`，答案前会打印模型的 `reasoning_content`（思考链）。

## 🧪 测试与评测

```bash
# 单元/接口测试（不需要模型）
uv run pytest -q

# 真实检索评测（需要 Ollama；默认读取 evaluation/cases.jsonl）
uv run python -m evaluation.runner --mode retrieval

# 存基线后做回归门禁：指标跌幅超过 --tolerance 即失败
uv run python -m evaluation.runner --mode retrieval --save-baseline
uv run python -m evaluation.runner --mode retrieval --baseline evaluation/reports/baseline.json
```

评测指标：Recall@K、MRR、证据命中率、拒答准确率、越权泄露数、时延分位。
种子集 33 条，覆盖单文档事实、数值、跨文档、权限拒答、超纲拒答、多轮指代。

## 📖 知识库内容

`data/knowledge_base.txt` 包含以下主题：

- 什么是 RAG：定义、工作流程、解决的三大 LLM 痛点
- LangChain 核心组件：Document / TextSplitters / Embeddings / VectorStore / LLM / Retriever
- 稠密检索（FAISS）与稀疏检索（BM25）的原理与差异
- 混合检索与 RRF 倒数排名融合算法
- LCEL（LangChain 表达式语言）简介
- 应用场景与扩展方向

## 📁 项目结构

```
langchain-rag-tutorial/
├── README.md                      # 本文件
├── main.py                        # 教学主线：单文件 RAG（FAISS + BM25 + RRF + 多轮 + 流式）
├── pyproject.toml / uv.lock       # 依赖与锁文件
├── .env.example                   # 环境变量模板（.env 已 gitignore）
├── data/
│   ├── knowledge_base.txt         # 教学知识库
│   └── gov_docs/                  # 政务语料（4 篇，front matter 标注部门/密级/生效日期）
├── app/                           # 后端：FastAPI + SSE + ACL + 审计
│   ├── users.py                   # 演示用户、口令校验、can_view()
│   ├── rag_service.py             # 密级解析 / 权限裁剪 / 检索 / 拒答 / 流式生成
│   ├── server.py                  # 路由 / 会话 / SSE / 审计 / 健康检查
│   └── static/index.html          # 零依赖单页（回退界面）
├── frontend/                      # 前端：React 18 + TypeScript + Vite 5
│   ├── src/                       # App / components / hooks / api / markdown
│   ├── package.json
│   └── dist/                      # 构建产物（gitignore；存在时由后端同源托管）
├── evaluation/                    # 评测台
│   ├── cases.jsonl                # 33 条种子用例（六类标签）
│   ├── cases.py / metrics.py      # 用例校验与指标
│   └── runner.py                  # 评测执行器（retrieval / answer）
├── docs/                          # 方案、架构、摄取设计、交付路线
├── faiss_index/                   # 教学索引（运行时生成，gitignore）
├── web_index/                     # 政务索引（运行时生成，gitignore）
├── logs/                          # 审计日志 audit.jsonl（gitignore）
└── tests/                         # pytest：test_main / test_web / test_eval
```

## 💡 核心代码流程

```
1. 加载文档 (load_documents)
       ↓
2. 文本分割 (RecursiveCharacterTextSplitter)
       ↓
   ┌───────────────────────────────┐
   │  3a. 向量嵌入 → FAISS 稠密索引  │
   │  3b. BM25 倒排索引（稀疏）     │
   └───────────────────────────────┘
       ↓
4. 混合检索 (hybrid_search)
   → FAISS Top-5 + BM25 Top-5
   → RRF 倒数排名融合
       ↓
5. 拼 Prompt + LLM 生成答案 (ChatOllama.invoke)
```

## 🚀 扩展方向

1. **更多数据源**：PDF、Word、网页
2. **多文档管理**：政务原型已按「文档 + 部门 + 密级」管理多文件语料
3. **聊天历史**：✅ 已实现（追问改写 + 最近 3 轮）
4. **Web UI**：✅ 已实现（React 18 + TS + Vite，`frontend/`；另保留零依赖单页作为回退）
5. **API 服务**：✅ 已实现（FastAPI + SSE 流式接口）
6. **权限与审计**：✅ 试点原型（`app/`），检索前裁剪 + JSONL 审计
7. **生产级混合检索**：本项目手写 RRF 便于讲清原理；落地时可直接用 LangChain 官方 [`EnsembleRetriever`](https://reference.langchain.com/python/langchain-classic/retrievers/ensemble/EnsembleRetriever)（langchain 1.x 位于 `langchain_classic` 包，内置 RRF 融合），组合 BM25 与向量检索器，省去自维护融合逻辑

## 📚 相关链接

- [LangChain 文档](https://python.langchain.com)
- [LangChain GitHub](https://github.com/langchain-ai/langchain)
- [Ollama 文档](https://ollama.ai/docs)
- [FAISS 文档](https://faiss.ai)
- [DeepSeek API 文档](https://api-docs.deepseek.com)
- [DeepSeek 定价](https://api-docs.deepseek.com/quick_start/pricing)
- [RAG 原理介绍](https://python.langchain.com/docs/tutorials/rag)