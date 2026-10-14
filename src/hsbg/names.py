"""日志自举的 `CardID → 中文名` 表（P2 的离线补充通道）。

为什么需要它：卡表（`cards.py`）给的是**完整官方名**，而游戏日志里出现的
往往是**对局内短名**。P0 实测一份 92 MB 日志可自举出 496 条，用卡表校验：

* 一致 493 条
* 不一致 2 条 —— 都是英雄皮肤：日志 `洛卡拉` vs 卡表 `奥术战士洛卡拉`；
  日志 `阮大师` vs 卡表 `神秘导师阮大师`
* 卡表缺失 1 条 —— `EDR_100t13e`（非战棋卡，日志里路过）

所以两层要**并存**：卡表是底，日志自举的短名优先（展示层用短名更贴近游戏内文本）。

缓存格式（`data/names_zhCN.json`）：

```json
{"version": 1, "source": "Power.log", "count": 496, "names": {"BG36_110": "欢悦"}}
```
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Iterable, Optional

from .cards import PACKAGE_DATA_DIR, CardDB, slugify_card_id

__all__ = [
    "NAME_DB_VERSION",
    "DEFAULT_NAME_DB_PATH",
    "collect_names_from_events",
    "save_names",
    "load_names",
    "load_name_overrides",
]

NAME_DB_VERSION = 1
DEFAULT_NAME_DB_PATH = PACKAGE_DATA_DIR / "names_zhCN.json"


def collect_names_from_events(events: Iterable) -> Dict[str, str]:
    """从事件流里抠出 `CardID → 中文名`。

    名字来自方括号实体形态（`Entity=[entityName=欢悦 id=312 … cardId=BG36_110 …]`）
    与 `META_DATA` 的 `Info[i]` 明细 —— 这些形态**同时带名字和 CardID**。
    """
    out: Dict[str, str] = {}
    for ev in events:
        ent = getattr(ev, "entity", None)
        if ent is None:
            continue
        cid = getattr(ent, "card_id", "") or ""
        nm = getattr(ent, "name", "") or ""
        if not cid or not nm:
            continue
        if nm.startswith("UNKNOWN ENTITY") or nm == "N0NAME":
            continue
        out.setdefault(cid, nm)
    return out


def save_names(names: Dict[str, str], path: Path = DEFAULT_NAME_DB_PATH,
               source: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": NAME_DB_VERSION,
        "source": source,
        "built_at": int(time.time()),
        "count": len(names),
        "names": dict(sorted(names.items())),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
    return path


def load_names(path: Path = DEFAULT_NAME_DB_PATH) -> Dict[str, str]:
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return {str(k): str(v) for k, v in (data.get("names") or {}).items()}


def load_name_overrides(path: Optional[Path] = None) -> Dict[str, str]:
    """展示层用的覆盖表：优先项目自带的自举表，其次工程根下第一份 Power.log。

    真实日志里那些**皮肤短名**只有自举通道能拿到，卡表拿不到，所以这里宁可用
    一份可能过期的自举表，也不退回卡表长名。
    """
    p = Path(path) if path is not None else DEFAULT_NAME_DB_PATH
    return load_names(p)


def refresh_from_log(log_paths: Iterable[Path], out: Path = DEFAULT_NAME_DB_PATH) -> Path:
    """从一份或多份 `Power.log` 重建自举表（供 `tools/` 调用）。"""
    from .parser import iter_file

    names: Dict[str, str] = {}
    sources = []
    for p in log_paths:
        p = Path(p)
        if not p.exists():
            continue
        sources.append(str(p))
        for cid, nm in collect_names_from_events(iter_file(p)).items():
            names.setdefault(cid, nm)
    return save_names(names, out, source=", ".join(sources))
