from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import sys
import threading
from typing import Callable

import psycopg
from psycopg import Connection

from ingest.db import connection
from ingest.queue import claim, complete, fail, heartbeat, reap_stale

Handler = Callable[[Connection, dict], None]
Hook = Callable[[Connection, dict], None]

logger = logging.getLogger("rag.worker")

# 数据库不可用时的轮询退避上限：再长会让恢复后的首轮任务明显延迟。
MAX_BACKOFF_SECONDS = 30.0


def run_job(conn: Connection, job: dict, handlers: dict[str, Handler],
            worker_id: str, dead_letter_hooks: dict[str, Hook] | None = None) -> bool:
    """执行单个已认领任务，返回是否成功。

    complete/fail 均带 worker_id 做所有权栅栏：任务若已被回收或由其他 worker
    接管，fail 返回 lost，此时不触发死信钩子（任务不归本 worker 处置）。
    """
    handler = handlers.get(job["kind"])
    if handler is None:
        fail(conn, job["id"], f"unknown job kind: {job['kind']}", worker_id=worker_id)
        return False
    try:
        handler(conn, job["payload"])
    except Exception as exc:  # noqa: BLE001 - 失败必须落库并重试
        conn.rollback()
        status = fail(conn, job["id"], f"{type(exc).__name__}: {exc}", worker_id=worker_id)
        if status == "lost":
            return False
        if status == "failed" and dead_letter_hooks and job["kind"] in dead_letter_hooks:
            dead_letter_hooks[job["kind"]](conn, job)
        return False
    complete(conn, job["id"], worker_id=worker_id)
    return True


def _start_heartbeat(job_id: int, worker_id: str) -> threading.Event:
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(30):
            try:
                with connection() as hb:
                    heartbeat(hb, job_id, worker_id)
            except Exception:  # noqa: BLE001 - 心跳失败交给回收机制
                pass

    threading.Thread(target=beat, daemon=True).start()
    return stop


def run_forever(handlers: dict[str, Handler], *, worker_id: str,
                poll_seconds: float = 1.0,
                dead_letter_hooks: dict[str, Hook] | None = None,
                stop: threading.Event | None = None) -> None:
    """常驻轮询；数据库短暂不可用时指数退避重试，收到停止信号后尽快返回。"""
    stop = stop or threading.Event()
    base = max(float(poll_seconds), 0.5)
    backoff = base
    while not stop.is_set():
        try:
            with connection() as conn:
                reap_stale(conn)
                job = claim(conn, worker_id)
        except psycopg.Error as exc:
            # 不能把数据库抖动变成 worker 退出：记日志、退避，期间仍可响应停机。
            delay = min(backoff, MAX_BACKOFF_SECONDS)
            logger.warning("worker %s poll failed, retry in %.1fs: %s",
                           worker_id, delay, exc)
            stop.wait(delay)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            continue
        backoff = base
        if job is None:
            stop.wait(poll_seconds)
            continue
        beat = _start_heartbeat(job["id"], worker_id)
        try:
            try:
                with connection() as conn:
                    run_job(conn, job, handlers, worker_id, dead_letter_hooks)
            except psycopg.Error as exc:
                # 取任务连接成功、执行连接失败：任务会由心跳超时回收重领，
                # 这里同样退避而不是让异常打穿主循环。
                delay = min(backoff, MAX_BACKOFF_SECONDS)
                logger.warning("worker %s job %s connection failed, retry in %.1fs: %s",
                               worker_id, job["id"], delay, exc)
                stop.wait(delay)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        finally:
            beat.set()


def run_once(handlers: dict[str, Handler], *, worker_id: str,
             dead_letter_hooks: dict[str, Hook] | None = None,
             max_jobs: int = 1000) -> int:
    """
    消费到「此刻没有可领取的任务」为止，返回处理条数。

    只领取 `run_after <= now()` 的任务：退避中的失败任务不会被 `--once` 强行提前执行，
    这是验收时「跑完一轮」与「把失败任务立刻重跑」的区别。
    """
    processed = 0
    while processed < max_jobs:
        with connection() as conn:
            reap_stale(conn)
            job = claim(conn, worker_id)
        if job is None:
            return processed
        stop = _start_heartbeat(job["id"], worker_id)
        try:
            with connection() as conn:
                run_job(conn, job, handlers, worker_id, dead_letter_hooks)
        finally:
            stop.set()
        processed += 1
    return processed


def _install_stop_handlers(stop: threading.Event) -> None:
    """SIGINT/SIGTERM 只置停止位，让主循环在 poll 等待中立即醒来退出。"""

    def _request_shutdown(signum, frame):  # noqa: ARG001 - signal 回调签名固定
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _request_shutdown)
        except (ValueError, OSError):
            # 非主线程（测试/嵌入场景）或平台不支持时保持默认行为
            continue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="摄取任务 worker（一进程一循环）")
    parser.add_argument("--worker-id", default=f"worker-{os.getpid()}")
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--once", action="store_true",
                        help="队列暂时为空就退出（验收/排障用）")
    args = parser.parse_args(argv)

    # Windows 控制台默认 GBK，中文启动日志与 argparse 错误提示都会 UnicodeEncodeError，
    # 必须先规整输出编码，再做任何可能打印中文的校验。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")

    # nan/inf/<=0 会让 sleep 抛异常或忙等，参数错误按 argparse 惯例以 2 退出
    if not math.isfinite(args.poll) or args.poll <= 0:
        parser.error("--poll 必须是大于 0 的有限数值")

    # 延迟导入：pipeline 会拉起解析器与嵌入依赖，参数写错时不必加载它们
    from ingest import pipeline

    if args.once:
        count = run_once(pipeline.HANDLERS, worker_id=args.worker_id,
                         dead_letter_hooks=pipeline.DEAD_LETTER_HOOKS)
        print(f"worker {args.worker_id} 处理 {count} 个任务后退出")
        return 0

    stop = threading.Event()
    _install_stop_handlers(stop)
    print(f"worker {args.worker_id} 已启动（poll={args.poll}s）")
    run_forever(pipeline.HANDLERS, worker_id=args.worker_id,
                poll_seconds=args.poll,
                dead_letter_hooks=pipeline.DEAD_LETTER_HOOKS,
                stop=stop)
    print(f"worker {args.worker_id} 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
