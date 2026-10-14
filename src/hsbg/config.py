"""全局配置与路径常量。

日志路径的事实（本机实测，2026-10-13）：
  - 日志根目录：/Applications/Hearthstone/Logs/
  - 会话目录名形如 Hearthstone_2026_10_10_21_11_09
  - Power.log 单会话可达 90-100 MB
  - /Applications/Hearthstone/log.config 不存在（走默认全量输出）
  - client.config 里 FileSizeLimit.Int=-1，即日志不轮转、无限增长
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: 默认日志根目录。可用环境变量 HSBG_LOG_ROOT 覆盖（便于测试与跨机）。
DEFAULT_LOG_ROOT = Path(
    os.environ.get(
        "HSBG_LOG_ROOT",
        "/Applications/Hearthstone/Logs",
    )
)

#: 会话目录前缀，后面跟 YYYY_MM_DD_HH_MM_SS。
SESSION_DIR_PREFIX = "Hearthstone_"

#: 我们主要关心的日志文件名。
POWER_LOG_NAME = "Power.log"

#: tail 轮询间隔（秒）。100 ms 足够跟上游戏节奏，又不会空转烧 CPU。
DEFAULT_POLL_INTERVAL = 0.1

#: 单次最多读取的字节数。防止一次 read 把 100 MB 全灌进内存。
DEFAULT_READ_CHUNK = 1 << 20  # 1 MiB


@dataclass(frozen=True)
class TailConfig:
    """tail 行为参数。"""

    poll_interval: float = DEFAULT_POLL_INTERVAL
    read_chunk: int = DEFAULT_READ_CHUNK
    #: 游戏重启时会新建会话目录，是否自动跟随到新目录。
    follow_new_session: bool = True


__all__ = [
    "DEFAULT_LOG_ROOT",
    "SESSION_DIR_PREFIX",
    "POWER_LOG_NAME",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_READ_CHUNK",
    "TailConfig",
]
