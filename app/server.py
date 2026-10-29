"""
FastAPI 接入层：登录会话、SSE 流式问答、审计日志。

安全设计要点（教学重点，生产替换项已在注释中标明）：

- 身份：演示用「选人登录」，服务端签发 itsdangerous 签名 Cookie（HttpOnly、SameSite=Lax）。
  真实系统应替换为统一身份认证（OIDC / CAS / LDAP）+ 短时令牌。
- 授权：所有权限判定都在 rag_service 检索前完成；接入层只负责「认证你是谁」。
- 审计：每次问答落一行 JSON 到 logs/audit.jsonl，包含用户、问题、改写后查询、
  命中切片（含密级）、是否拒答、答案全文、耗时、模型版本。
- 内容安全、限流、密评等尚未实现，属于方案中的 P1（见 docs/政府级RAG改造方案.md）。

启动：uv run uvicorn app.server:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

from app import audit as _audit
from app.rag_service import BASE_DIR, RagService, core, max_distance
from app.users import DEMO_USERS, User, authenticate, get_user

logger = logging.getLogger("rag.server")

COOKIE_NAME = "rag_session"
SESSION_MAX_AGE = 8 * 3600  # 8 小时（服务端同样校验，不再只依赖浏览器 cookie 过期）
STATIC_DIR = Path(__file__).resolve().parent / "static"
# React 生产构建产物（frontend/dist）。存在时由本服务直接托管，否则回退到单文件原型页面。
DIST_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
AUDIT_FILE = _audit.AUDIT_FILE

# 反向代理后取 X-Forwarded-For 需要显式开启，否则客户端可伪造来源 IP。
TRUST_PROXY = os.getenv("TRUST_PROXY", "0").strip().lower() in {"1", "true", "yes"}

# 登录失败限速：连续 5 次失败锁定 30 分钟（与演示语料 gov-004 的口令要求口径一致）。
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 30 * 60


def _env_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，使用默认值 %d", name, raw, default)
        return default
    return value if value > 0 else default


# 问答并发上限：同步端点跑在线程池里，每个请求占一个线程直到生成结束。
# 不设上限时，少量慢请求就能打满线程池，把登录、/api/me 等轻接口一起拖死。
MAX_CONCURRENT_CHATS = _env_positive_int("MAX_CONCURRENT_CHATS", 4)
_chat_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CHATS)

# 会话密钥：生产必须从密钥管理系统注入。缺失时随机生成，仅适用于单进程本地演示。
_SESSION_SECRET_FROM_ENV = os.getenv("SESSION_SECRET")
SESSION_SECRET = _SESSION_SECRET_FROM_ENV or secrets.token_hex(32)
if not _SESSION_SECRET_FROM_ENV:
    logger.warning(
        "SESSION_SECRET 未配置：已使用临时随机密钥。重启后全部会话失效，"
        "多进程/多副本部署会因密钥不同出现随机 401。"
    )
serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="rag-session")


# ---------- 服务实例（延迟初始化） ----------

_service: RagService | None = None
_service_error: str | None = None
_init_lock = threading.Lock()


def get_service() -> RagService:
    """首次请求时初始化（加载语料、校验/构建索引、连接 Ollama）。"""
    global _service, _service_error
    if _service is None:
        with _init_lock:  # 同步端点跑在线程池里，避免并发首请求重复建索引
            if _service is None:
                try:
                    _service = RagService()
                    _service_error = None
                except Exception as exc:  # noqa: BLE001 - 初始化失败要变成可读的 503
                    _service_error = f"{type(exc).__name__}: {exc}"
                    raise HTTPException(
                        status_code=503,
                        detail=f"服务初始化失败：{_service_error}。请确认 Ollama 已启动并已拉取模型。",
                    ) from exc
    return _service


# ---------- 审计 ----------


def audit_ready(audit_file: Path | None = None) -> tuple[bool, str]:
    """
    审计可用性预检（fail-closed 用）。

    审计写不进去时继续对外提供服务，等于「有问无痕」。这里在生成答案前先探一次
    可写性，失败则拒绝本次问答，而不是让业务在无审计状态下运行。
    """
    target = Path(audit_file or AUDIT_FILE)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8"):
            pass
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def write_audit(record: dict, audit_file: Path | None = None) -> None:
    """兼容包装：审计实现已抽到 app/audit.py，供 API 与 worker 共用。"""
    _audit.write_audit(record, audit_file or AUDIT_FILE)


def _safe_audit(record: dict) -> None:
    """
    认证/会话类事件的审计：写入失败只记系统日志，不阻断请求。

    登录若因审计盘满而整体不可用，会把系统锁死在「谁也进不来」的状态；
    问答路径另有 fail-closed 预检（见 chat），保证「有问必有痕」。
    """
    try:
        write_audit(record)
    except OSError as exc:
        logger.error("audit.write_failed event=%s err=%s", record.get("event"), exc)


# ---------- 认证 ----------


_REVOKED_LOCK = threading.Lock()
_REVOKED: dict[str, float] = {}  # jti -> 过期时间戳（秒）


def _revoke(jti: str | None) -> None:
    """服务端注销会话。注：进程内黑名单，多副本部署需换 Redis 等共享存储。"""
    if not jti:
        return
    now = time.time()
    with _REVOKED_LOCK:
        # 顺带清理已自然过期的记录，避免黑名单无限增长
        for key in [k for k, exp in _REVOKED.items() if exp <= now]:
            _REVOKED.pop(key, None)
        _REVOKED[jti] = now + SESSION_MAX_AGE


def _is_revoked(jti: str | None) -> bool:
    if not jti:
        return True
    with _REVOKED_LOCK:
        exp = _REVOKED.get(jti)
        if exp is None:
            return False
        if exp <= time.time():
            _REVOKED.pop(jti, None)
            return False
        return True


def _new_session(username: str) -> str:
    """签发带 jti 的会话令牌；过期由服务端 loads(max_age=...) 校验。"""
    return serializer.dumps({"u": username, "jti": uuid4().hex})


def _session_of(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = serializer.loads(token, max_age=SESSION_MAX_AGE)
    except SignatureExpired as exc:
        raise HTTPException(status_code=401, detail="会话已过期，请重新登录") from exc
    except BadSignature as exc:
        raise HTTPException(status_code=401, detail="会话无效，请重新登录") from exc
    if not isinstance(payload, dict) or not payload.get("u"):
        # 旧格式（仅 username 字符串）没有签发时间与 jti，无法校验过期/吊销，一律拒绝
        raise HTTPException(status_code=401, detail="会话格式已升级，请重新登录")
    if _is_revoked(payload.get("jti")):
        raise HTTPException(status_code=401, detail="会话已注销，请重新登录")
    return payload


def current_user(request: Request) -> User:
    payload = _session_of(request)
    user = get_user(str(payload["u"]))
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


_LOGIN_LOCK = threading.Lock()
_LOGIN_FAILURES: dict[str, list[float]] = {}


def _login_locked_for(username: str) -> int:
    """返回剩余锁定秒数；0 表示未锁定。"""
    now = time.time()
    with _LOGIN_LOCK:
        attempts = [t for t in _LOGIN_FAILURES.get(username, []) if now - t < LOGIN_LOCK_SECONDS]
        if attempts:
            _LOGIN_FAILURES[username] = attempts
        else:
            _LOGIN_FAILURES.pop(username, None)
        if len(attempts) < LOGIN_MAX_FAILURES:
            return 0
        return max(1, int(LOGIN_LOCK_SECONDS - (now - attempts[-1])))


def _record_login_failure(username: str) -> None:
    now = time.time()
    with _LOGIN_LOCK:
        attempts = [t for t in _LOGIN_FAILURES.get(username, []) if now - t < LOGIN_LOCK_SECONDS]
        attempts.append(now)
        _LOGIN_FAILURES[username] = attempts


def _clear_login_failures(username: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILURES.pop(username, None)


def _client_ip(request: Request) -> str:
    """开启 TRUST_PROXY 时取代理链首个地址，否则用直连地址（防伪造）。"""
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


# ---------- 请求模型 ----------


class LoginPayload(BaseModel):
    username: str
    password: str = ""


class ChatPayload(BaseModel):
    question: str


class HistoryTurn(BaseModel):
    question: str
    answer: str


class HistorySeedPayload(BaseModel):
    turns: list[HistoryTurn] = []


# ---------- 应用 ----------

app = FastAPI(title="政务知识问答（试点原型）", version="0.1.0")

from ingest.errors import IngestError


@app.exception_handler(IngestError)
async def _ingest_error_handler(request: Request, exc: IngestError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"code": exc.code, "detail": str(exc), **exc.detail},
    )


@app.get("/")
def index() -> FileResponse:
    # 生产构建存在时优先托管 React 产物，否则回退到单文件原型页面
    dist_index = DIST_DIR / "index.html"
    if dist_index.exists():
        return FileResponse(dist_index)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/users")
def list_users() -> dict:
    """
    演示账号列表（真实系统没有这个接口，用户来自统一身份认证）。

    只返回姓名/部门/职务，不返回最高密级——登录前不应让任何人知道
    「哪个账号能看秘密」。
    """
    return {"users": [user.to_public_dict() for user in DEMO_USERS.values()]}


@app.post("/api/login")
def login(payload: LoginPayload, request: Request, response: Response) -> dict:
    username = payload.username.strip()
    ip = _client_ip(request)

    locked_for = _login_locked_for(username)
    if locked_for:
        _safe_audit({
            "event": "auth.login", "status": "locked", "username": username,
            "ip": ip, "reason": "too_many_failures", "retry_after_s": locked_for,
        })
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请 {max(1, locked_for // 60)} 分钟后再试",
            headers={"Retry-After": str(locked_for)},
        )

    user = authenticate(username, payload.password)
    if user is None:
        _record_login_failure(username)
        _safe_audit({
            "event": "auth.login", "status": "failed", "username": username,
            "ip": ip, "reason": "bad_credentials",
        })
        # 不区分「账号不存在」与「口令错误」，避免账号枚举
        raise HTTPException(status_code=401, detail="账号或口令错误")

    _clear_login_failures(username)
    _safe_audit({
        "event": "auth.login", "status": "ok", "username": username,
        "dept": user.dept, "max_level": user.max_level, "ip": ip,
    })
    response.set_cookie(
        COOKIE_NAME,
        _new_session(user.username),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
        # 内网 HTTP 部署时不能设 secure=True，否则浏览器不回传 Cookie；
        # 生产（HTTPS）必须开启 secure=True。
    )
    return {"user": user.to_dict()}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    payload: dict = {}
    try:
        payload = _session_of(request)
    except HTTPException:
        payload = {}  # 未登录/已过期也允许登出，幂等
    _revoke(payload.get("jti"))
    _safe_audit({
        "event": "auth.logout", "status": "ok",
        "username": payload.get("u"), "ip": _client_ip(request),
    })
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user)) -> dict:
    service = get_service()
    return {
        "user": user.to_dict(),
        "visible_docs": service.visible_docs(user),
        "stats": service.stats(),
    }


@app.get("/api/health")
def health():
    """
    健康检查：未就绪返回 503。

    原实现无论是否初始化都返回 200 + ok=false，K8s/LB 探针会把「未就绪」
    当成「健康」，流量在冷启动或 Ollama 故障时仍然打进来。

    仅当配置了 INGEST_DB_URL（pg 模式）才探测数据库与队列；FAISS 单机模式下
    没有 DB，不能因为「连不上库」把原有部署判为不健康。
    """
    if _service is None:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": _service_error or "服务未初始化（语料/索引尚未加载）",
            },
        )
    stats = _service.stats()
    issues = stats.get("load_issues", [])
    payload = {
        "ok": True,
        "docs": stats["docs"],
        "chunks": stats["chunks"],
        "skipped_docs": len(issues),
        "skipped_files": [item["file"] for item in issues],
        "max_distance": max_distance(),
        # faiss 模式是 EMBED_MODEL，pg 模式是 INGEST_EMBED_MODEL——由 stats 给出，别在这里猜
        "embed_model": stats["embed_model"],
    }
    from ingest.config import get_settings

    if get_settings().db_url:
        try:
            from ingest.db import connection
            from ingest.queue import queue_depth

            with connection() as conn:
                conn.execute("SELECT 1")
                payload["queue_depth"] = queue_depth(conn)
            payload["db"] = "ok"
        except Exception as exc:  # noqa: BLE001 - 探针要把依赖故障暴露为 503
            payload["ok"] = False
            payload["db"] = f"error: {type(exc).__name__}"
            return JSONResponse(status_code=503, content=payload)
    return payload


@app.post("/api/history/clear")
def clear_history(request: Request, user: User = Depends(current_user)) -> dict:
    get_service().clear_history(user.username)
    _safe_audit({
        "event": "history.clear", "status": "ok",
        "username": user.username, "ip": _client_ip(request),
    })
    return {"ok": True}


@app.post("/api/history/seed")
def seed_history(
    payload: HistorySeedPayload,
    request: Request,
    user: User = Depends(current_user),
) -> dict:
    """
    回填历史轮次。

    前端把本地保存的对话恢复到界面后，需要把轮次同步回服务端，
    否则「那报销标准呢？」这类追问会因缺少上下文而被改写失败。
    """
    turns = [(t.question, t.answer) for t in payload.turns if t.question and t.answer]
    if turns:
        get_service().restore_history(user.username, turns)
        _safe_audit({
            "event": "history.seed", "status": "ok", "turns": len(turns),
            "username": user.username, "ip": _client_ip(request),
        })
    return {"ok": True, "seeded": len(turns)}


@app.get("/api/chunks/{chunk_id}")
def chunk_detail(chunk_id: str, request: Request, user: User = Depends(current_user)) -> dict:
    """
    取回单个命中切片的原文，供前端「查看原文」使用。

    权限判定在服务层用检索同一套 can_view 完成；不可见时统一返回 404，
    不区分「不存在」与「无权查看」，避免成为枚举密级归属的旁路。
    """
    detail = get_service().chunk_detail(user, chunk_id)
    if detail is None:
        _safe_audit({
            "event": "chunk.read", "status": "denied",
            "username": user.username, "chunk_id": chunk_id, "ip": _client_ip(request),
        })
        raise HTTPException(status_code=404, detail="原文不存在或无权查看")
    _safe_audit({
        "event": "chunk.read", "status": "ok",
        "username": user.username, "chunk_id": chunk_id, "ip": _client_ip(request),
    })
    return detail


def _sse(event: dict) -> str:
    """SSE 帧：`data: {json}\n\n`。用 JSON 承载结构化事件，前端按 type 分发。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
def chat(
    payload: ChatPayload,
    request: Request,
    user: User = Depends(current_user),
) -> StreamingResponse:
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    if len(question) > 500:
        raise HTTPException(status_code=400, detail="问题过长（上限 500 字）")

    # 审计 fail-closed：写不进去就不提供问答，避免「有问无痕」。
    audit_ok, audit_err = audit_ready()
    if not audit_ok:
        logger.error("audit 不可写，拒绝问答请求 request_id=%s err=%s", uuid4().hex, audit_err)
        raise HTTPException(status_code=503, detail="审计通道不可用，已按合规要求暂停问答服务")

    service = get_service()
    history = service.history(user.username)
    request_id = uuid4().hex
    started = time.perf_counter()
    rewritten = question
    ip = _client_ip(request)

    def event_stream() -> Iterator[str]:
        nonlocal rewritten

        # 并发闸门：拿不到槽位就回一个 error 事件，不排队、不占线程。
        if not _chat_slots.acquire(blocking=False):
            logger.warning("chat 并发已满（上限 %d），拒绝新请求", MAX_CONCURRENT_CHATS)
            yield _sse({"type": "error", "message": "当前问答请求过多，请稍后重试"})
            return

        answer_parts: list[str] = []
        citations: list[dict] = []
        refusal: dict | None = None
        status = "ok"
        error_text: str | None = None
        deadline = started + float(core.LLM_TIMEOUT_S)

        try:
            for event in service.answer_stream(user, question, history):
                if time.perf_counter() > deadline:
                    raise TimeoutError(f"本次生成超过 {core.LLM_TIMEOUT_S:.0f}s 预算")
                if event["type"] == "rewrite":
                    rewritten = event["to"]
                elif event["type"] == "token":
                    answer_parts.append(event["text"])
                elif event["type"] == "citations":
                    citations = event["items"]
                elif event["type"] == "refusal":
                    refusal = event
                yield _sse(event)
        except GeneratorExit:
            # 客户端主动停止/断开：审计要如实记录「未生成完整答案」
            status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - 异常也要变成前端可读事件
            status = "error"
            error_text = f"{type(exc).__name__}: {exc}"
            logger.exception("chat 生成失败 request_id=%s", request_id)
            # 只回传 request_id，不把内部异常文本（可能含内网地址/模型端点）暴露给前端
            yield _sse({"type": "error", "message": f"生成失败，请稍后重试（请求号 {request_id}）"})
        finally:
            _chat_slots.release()
            if status == "ok" and refusal is not None:
                status = "refused"

            visible_doc_ids: list[str] = []
            llm_model = embed_model = None
            try:
                visible_doc_ids = [d["doc_id"] for d in service.visible_docs(user)]
                stats = service.stats()
                llm_model = stats["llm_model"]
                embed_model = stats["embed_model"]
            except Exception:  # noqa: BLE001 - 审计附加信息失败不能掩盖主流程
                logger.exception("审计附加信息采集失败 request_id=%s", request_id)

            try:
                write_audit({
                    "event": "chat",
                    "status": status,
                    "error": error_text,
                    "request_id": request_id,
                    "user": user.username,
                    "dept": user.dept,
                    "max_level": user.max_level,
                    "ip": ip,
                    "question": question,
                    "rewritten_query": rewritten,
                    "refused": refusal is not None,
                    "refusal_reason": refusal["reason"] if refusal else None,
                    "visible_docs": visible_doc_ids,
                    "citations": [
                        {
                            "chunk_id": item["chunk_id"],
                            "doc_id": item["doc_id"],
                            "classification": item["classification"],
                            "dept": item["dept"],
                        }
                        for item in citations
                    ],
                    "answer": "".join(answer_parts),
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "llm": llm_model,
                    "embed_model": embed_model,
                })
            except OSError as exc:
                # 预检通过但写入失败（磁盘满/权限变更）：响应无法回滚，至少留下系统日志
                logger.error("audit.write_failed request_id=%s err=%s", request_id, exc)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关闭 Nginx 缓冲（若前置代理）
            "X-Request-Id": request_id,
        },
    )


# 挂载 React 构建产物中的静态资源（dist/assets）。仅当该目录存在时挂载，
# 否则 StaticFiles 会在启动时因目录缺失而报错。API 路由（/api/*）已在上方注册，不受影响。
if (DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(DIST_DIR / "assets")), name="dist-assets")


from app.routers import documents as documents_api
from app.routers import jobs as jobs_api
from app.routers import review as review_api
from app.routers import source as source_api
from app.routers import templates as templates_api

app.include_router(documents_api.router)
app.include_router(review_api.router)
app.include_router(templates_api.router)
app.include_router(jobs_api.router)
app.include_router(source_api.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.server:app", host="127.0.0.1", port=8000, reload=False)
