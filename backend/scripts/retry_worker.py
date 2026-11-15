"""失败提交的后台补偿进程（独立启动，不进 API 进程）。

用法（工作目录 backend）：
    ..\\.venv\\Scripts\\python.exe scripts\\retry_worker.py            # 常驻，按间隔扫描
    ..\\.venv\\Scripts\\python.exe scripts\\retry_worker.py --once     # 只扫一轮（排查用）
    ..\\.venv\\Scripts\\python.exe scripts\\retry_worker.py --interval 5

为什么单独起进程而不是让 uvicorn 顺手跑线程：
- 多 worker 部署时 N 个进程各自扫描，退避与上限只能靠数据库兜底，纯属浪费；
- --reload 会让线程随进程重启反复起停，重试节奏完全不可控；
- 这个进程挂了不该影响答题接口，反过来 API 重启也不该丢掉补偿节奏。

多实例安全：抢占是数据库条件更新（processing_status='FAILED' AND retryable
AND next_retry_at<=now AND 抢占未过期），rowcount==1 的才是执行者。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# 允许以脚本方式直接运行：把 backend 目录加进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

from app.core.config import get_settings  # noqa: E402
from app.core.logging import get_logger, setup_logging  # noqa: E402
from app.infrastructure.db import get_session_factory  # noqa: E402
from app.services import retry_worker  # noqa: E402

log = get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="失败提交的后台补偿进程")
    parser.add_argument("--once", action="store_true", help="只扫一轮就退出")
    parser.add_argument("--interval", type=int, default=None, help="扫描间隔（秒）")
    parser.add_argument("--batch", type=int, default=None, help="每轮最多处理条数")
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    interval = args.interval or retry_worker.worker_interval_seconds()
    batch = args.batch or max(int(settings.retry_worker_batch or 5), 1)
    # sessionmaker 本身可调用：run_once 内部按条调用它拿新会话（一条一事务）
    db_factory = get_session_factory()

    log.info(
        "补偿进程启动：间隔 %ss，每轮最多 %s 条，LLM_MOCK=%s",
        interval, batch, settings.llm_mock,
    )

    while True:
        started = time.monotonic()
        try:
            stats = retry_worker.run_once(db_factory=db_factory, limit=batch)
            if stats["scanned"]:
                log.info("一轮补偿结束：%s", stats)
        except Exception as exc:  # noqa: BLE001 —— 进程级兜底：一轮失败不能退出
            log.exception("补偿扫描失败: %s", type(exc).__name__)

        if args.once:
            return 0
        elapsed = time.monotonic() - started
        time.sleep(max(interval - elapsed, 1))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log.info("补偿进程收到中断，退出")
