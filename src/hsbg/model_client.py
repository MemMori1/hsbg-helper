#!/usr/bin/env python3
"""M2：把模型咨询转给 **llm-odds 插件**（独立仓库，子进程 + JSON 契约）。

为什么用子进程而不是 import
---------------------------
llm-odds 是独立发布节奏的仓库，宿主只依赖它的 **JSON 契约 v1**
（`schemas/ask.request.v1 / ask.response.v1`）。子进程边界 = 版本边界：
插件没装 / 崩了 / 换实现，hsbg 都只看到"模型不可用"，走原有降级路径。

找插件的顺序：`HSBG_ODDS_BIN` → `PATH` 上的 `llm-odds` →
兄弟目录 `../llm-odds/bin/llm-odds`（本仓与插件仓并列的情形）。
找不到就由 `advisor_from_args` 退回旧的直连实现（M4-lite：保留，不再演进）。

环境变量
--------
- `HSBG_ODDS_BIN`      插件可执行文件（最高优先级）
- `HSBG_ODDS_PROFILE`  模型档位名（下发给插件 `params.profile`）
- `HSBG_ODDS_REGISTRY` 模型注册表路径（转成插件的 `LLM_ODDS_REGISTRY`）
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .provider import (Advisor, Answer, ModelUnavailable, Question,
                       answer_from_probs)


def find_llm_odds() -> Optional[str]:
    """按 env → PATH → 兄弟目录 的顺序找 llm-odds 可执行文件。"""
    env = os.environ.get("HSBG_ODDS_BIN")
    if env and Path(env).is_file():
        return env
    which = shutil.which("llm-odds")
    if which:
        return which
    # 兄弟目录：例如 ~/Desktop/project/{hsbg-helper, llm-odds}
    project = Path(__file__).resolve().parents[2].parent
    sibling = project / "llm-odds" / "bin" / "llm-odds"
    if sibling.is_file():
        return str(sibling)
    return None


def _raw_options(q: Question) -> List[Tuple[str, str]]:
    """选项转成"裸文本"（不带 `id: ` 前缀）——前缀交给插件的 option_prefix 渲染。

    ⚠️ 这样拼出的 prompt 与旧直连实现**逐字节一致**（已对照验收过）：
    `question.options()` 里的 `"id: 描述"` = 插件 `prefix=true` 的渲染结果。
    """
    if q.qtype == "noul":
        return []  # 插件对 noul 固定 true/false 两项，不必传
    if q.qtype == "choice":
        crit = q.criteria
        if isinstance(crit, dict):
            return [(str(k), str(v or k)) for k, v in crit.items()]
        return [(str(c), str(c)) for c in crit]
    return [(str(i), str(lvl)) for i, lvl in enumerate(q.criteria)]


class OddsAdvisor(Advisor):
    """llm-odds 插件顾问：一次 `decide_many` = 一个 `ask-many` 子进程。"""

    name = "llm-odds"

    def __init__(self, bin_path: str, profile: Optional[str] = None,
                 timeout: float = 60.0,
                 registry: Optional[str] = None) -> None:
        self.bin = bin_path
        self.profile = profile or os.environ.get("HSBG_ODDS_PROFILE") or ""
        self.registry = registry or os.environ.get("HSBG_ODDS_REGISTRY") or ""
        self.timeout = float(timeout or 60.0)
        #: 有字母掉出 top-k 的提问数（与 JevK5Client.missing 同名，面板/CLI 兼容）
        self.missing = 0
        self._avail = None  # (单调时间, bool)
        self._avail_ttl = 30.0

    # -- 子进程 --------------------------------------------------------

    def _env(self) -> dict:
        env = os.environ.copy()
        if self.registry:
            env["LLM_ODDS_REGISTRY"] = self.registry
        return env

    def _run(self, args: Sequence[str], payload: Optional[dict] = None,
             timeout: Optional[float] = None):
        """跑一次插件；返回 (rc, stdout, stderr)。失败一律 ModelUnavailable。"""
        cmd = [self.bin] + list(args)
        wait = self.timeout if timeout is None else timeout
        try:
            proc = subprocess.run(
                cmd,
                input=None if payload is None else json.dumps(payload),
                capture_output=True, text=True, encoding="utf-8",
                env=self._env(), timeout=wait)
        except subprocess.TimeoutExpired as e:
            raise ModelUnavailable("llm-odds 子进程超时（%ss）" % wait) from e
        except OSError as e:
            raise ModelUnavailable("起不了 llm-odds（%s）：%s" % (self.bin, e)) from e
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    @staticmethod
    def _parse(stdout: str):
        try:
            return json.loads(stdout)
        except ValueError as e:
            raise ModelUnavailable("llm-odds 没吐出 JSON：%s"
                                   % (stdout or "")[:200]) from e

    # -- Advisor 接口 ---------------------------------------------------

    def available(self) -> bool:
        """看插件注册表里默认档位是否可用（30s 缓存，别每帧都探）。"""
        now = time.monotonic()
        if self._avail and (now - self._avail[0]) < self._avail_ttl:
            return self._avail[1]
        ok = False
        try:
            rc, out, _ = self._run(["models", "--json"],
                                   timeout=min(self.timeout, 10.0))
            if rc == 0:
                ok = bool((self._parse(out) or {}).get("default_available"))
        except ModelUnavailable:
            ok = False
        self._avail = (now, ok)
        return ok

    def decide(self, state: str, question: Question) -> Answer:
        out = self.decide_many(state, [question])
        if not out:
            raise ModelUnavailable("llm-odds 没给出「%s」的答案"
                                   % (question.key or question.qtype))
        return out[0]

    def decide_many(self, state: str,
                    questions: Sequence[Question]) -> List[Answer]:
        qs = list(questions)
        if not qs:
            return []
        items = []
        for i, q in enumerate(qs):
            item = {"v": 1, "id": q.key or ("q%d" % i), "task": q.qtype,
                    "evidence": state, "criterion": q.instructions}
            opts = _raw_options(q)
            if opts:
                item["options"] = [{"id": oid, "text": txt}
                                   for oid, txt in opts]
            items.append(item)
        params = {"timeout_s": self.timeout}  # type: dict
        if self.profile:
            params["profile"] = self.profile
        payload = {"v": 1, "items": items, "params": params}
        # 子进程上限 = 单题超时 × (题数 + 2) 余量：ask-many 是串行问的
        rc, out, err = self._run(["ask-many", "-"], payload,
                                 timeout=self.timeout * (len(items) + 2))
        resp = self._parse(out)
        if not isinstance(resp, dict):
            raise ModelUnavailable("llm-odds 响应不是 JSON 对象")
        if resp.get("results") is None and resp.get("error"):
            e = resp.get("error") or {}
            raise ModelUnavailable("llm-odds 拒绝请求：[%s] %s"
                                   % (e.get("code", "?"), e.get("message", "")))
        results = resp.get("results") or []
        if len(results) != len(items):
            raise ModelUnavailable("llm-odds 结果数量不对：%d != %d"
                                   % (len(results), len(items)))
        answers = []  # type: List[Answer]
        errors = []
        for q, r in zip(qs, results):
            if not isinstance(r, dict) or not r.get("ok"):
                e = ((r or {}).get("error") or {})
                errors.append("%s=%s" % (q.key or "?", e.get("code", "?")))
                continue
            metrics = r.get("metrics") or {}
            try:
                a = answer_from_probs(
                    q, r.get("probs") or {},
                    tokens=int(metrics.get("tokens_evaluated") or 0),
                    seconds=float(metrics.get("elapsed_ms") or 0.0) / 1000.0)
            except ValueError:
                errors.append("%s=空分布" % (q.key or "?"))
                continue
            a.truncated = int(metrics.get("truncated") or 0)
            if int(metrics.get("missing_letters") or 0) > 0:
                self.missing += 1
            answers.append(a)
        if not answers:
            # 全失败才当"模型不可用"；部分失败保留成功的（consult 边答边降级）
            raise ModelUnavailable("llm-odds 全部失败（%s）"
                                   % ("；".join(errors) or "无结果"))
        return answers
