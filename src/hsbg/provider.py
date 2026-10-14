#!/usr/bin/env python3
"""P4：模型顾问层——JevK5 的 System One 协议（本机 llama-server 上的真·logprob 读法）。

红线（需求文档 §2.3）：这一层只**出建议**，不留任何执行接口。

为什么不用 LM Studio 的自带服务
--------------------------------
实测（2026-10-14）LM Studio 6324 端口的 `/v1/completions` 与 `/api/v0/completions`
都**不支持 logprobs / n_probs**（带 `logprobs` 或 `n_probs` 参数直接 400，
不带则 `logprobs: null`）。而 JevK5 的核心是"在答案位置取选项字母的 softmax"，
拿不到 logprob 就只能退化成生成式猜字母，置信度全丢。

好在 LM Studio 自带 llama.cpp 引擎（`llama.cpp-mac-arm64-apple-metal-advsimd@2.56.0`），
其中的 `llama-server` 原生支持 `n_probs`：`/completion` 返回
`completion_probabilities[0].top_logprobs`。所以走它，读法与官方 `jevk5.gguf.JevK5GGUF`
**逐字节一致**（同一 prompt 模板、同一 tokenize 路径、同一温度校准）。

协议（来自官方 `jevk5/prompt.py`，Apache-2.0，规则已抄录在此）
------------------------------------------------------------------
- system：固定一句话，要求"只回选项的大写字母，不要解释"。
- user：一个 JSON，形如
  `{"evidence": "...", "criterion": "...", "options": [{"letter": "A", "description": "..."}]}`
- 模板：Qwen3.5 的 chat template 关掉 thinking 后**预填空 think 块**：
  `<|im_start|>assistant\n<think>\n\n</think>\n\n` ← 答案位置的字母正是下一个 token。
- `n_predict=1` + `n_probs≥16` → 一次前向即可拿到 16 个字母的 logprob。
- 概率 = `softmax(logprob / temperature)`，温度按模型档位查表（2B v0.2 = 1.42）。
- 超过 16 个选项要分趟（官方 `spread` 的 knockout）—— 本项目最多十几张卡，先不实现，
  超出时**降级为只保留前 16 个**并在结果里标记（不静默丢）。

⚠️ 必须先把 prompt 用 `/tokenize` 转成 token id 再喂 `/completion`：直接把字符串当
prompt 传，`<|im_start|>` 会被拆成 6 个普通字符，模板结构全废（官方注释原话）。
"""
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: 官方 prompt.py 的字母表
LETTERS = "ABCDEFGHIJKLMNOP"

#: 官方 System One 的 system 消息（逐字抄自 jevk5/prompt.py）
SYSTEM_MESSAGE = (
    "Apply the supplied criterion to the supplied evidence. Choose exactly one listed option. "
    "Respond with only its uppercase letter, with no explanation or reasoning."
)

#: Qwen3.5 chat template + thinking off（官方 prompt.py 的 CHAT_TEMPLATE）
CHAT_TEMPLATE = (
    "<|im_start|>system\n{system}<|im_end|>\n"
    "<|im_start|>user\n{user}<|im_end|>\n"
    "<|im_start|>assistant\n<think>\n\n</think>\n\n"
)

#: 各档位的校准温度（来自官方 gguf.py 文档串与 JevK5-GGUF 卡）
CALIBRATION_TEMPERATURE = {
    "2b": 1.42,
    "4b": 1.367,          # v0.3
    "4b-v0.2": 1.532,     # v0.2 默认
    "9b": 1.089,
}

#: 字母不在 top-k 里时的兜底扣分（官方 MISSING_MARGIN）
MISSING_MARGIN = 2.0

DEFAULT_URL = "http://127.0.0.1:8090"


class ModelUnavailable(RuntimeError):
    """连不上本地模型服务 / 服务报错。上层据此降级为纯规则。"""


# ---------------------------------------------------------------------------
# prompt 构造
# ---------------------------------------------------------------------------

def build_payload(state: str, criterion: str,
                  options: Sequence[str]) -> dict:
    """官方 `prompt.messages` 的 user 消息载荷。"""
    return {
        "evidence": state,
        "criterion": criterion,
        "options": [{"letter": LETTERS[i], "description": d}
                    for i, d in enumerate(options)],
    }


def build_prompt(state: str, criterion: str,
                 options: Sequence[str]) -> str:
    """完整 prompt 字符串（含模板），供 `/tokenize` 用。"""
    user = json.dumps(build_payload(state, criterion, options),
                      ensure_ascii=False)
    return CHAT_TEMPLATE.format(system=SYSTEM_MESSAGE, user=user)


# ---------------------------------------------------------------------------
# 题型
# ---------------------------------------------------------------------------

@dataclass
class Question:
    """一个类型化的提问。

    `qtype`:
      - `noul`   是/否，答案取 `P(true)`
      - `choice` 从 `criteria` 里选一个
      - `score`  对 `criteria`（档位描述列表）打分，答案是档位序号的期望
    """
    qtype: str
    instructions: str
    criteria: Sequence[str] = ()
    key: str = ""

    def options(self) -> List[Tuple[str, str]]:
        """(选项 id, 选项文本)。文本统一写成 `"id: 描述"`（对齐官方 JevBench 映射）。"""
        if self.qtype == "noul":
            return [("true", "true: The proposition is true."),
                    ("false", "false: The proposition is false.")]
        if self.qtype == "choice":
            crit = self.criteria
            if isinstance(crit, dict):
                return [(k, f"{k}: {v or k}") for k, v in crit.items()]
            return [(str(c), f"{c}: {c}") for c in crit]
        return [(str(i), f"{i}: {lvl}") for i, lvl in enumerate(self.criteria)]


@dataclass
class Answer:
    """一个类型化回答。"""
    key: str
    qtype: str
    confidence: float
    probabilities: Dict[str, float] = field(default_factory=dict)
    choice: Optional[str] = None
    noul: Optional[float] = None
    score: Optional[float] = None
    input_tokens: int = 0
    seconds: float = 0.0
    #: `choice` 题选中项的字母（A/B/C…），便于 UI 说"模型选了第 N 项"
    letter: str = ""
    #: 选项超过 16 个被截断时的提示（本项目一般不会触发）
    truncated: int = 0

    def describe(self) -> str:
        if self.qtype == "noul":
            return "是" if (self.noul or 0) >= 0.5 else "否"
        if self.qtype == "choice":
            return self.choice or "?"
        if self.score is not None:
            return "%.2f" % self.score
        return "?"


def answer_from_probs(question: Question, probs: Dict[str, float],
                      tokens: int = 0, seconds: float = 0.0) -> Answer:
    """官方 `prompt.answer` 的回答形态。"""
    if not probs:
        raise ValueError("空的概率分布")
    a = Answer(key=question.key or question.instructions,
               qtype=question.qtype,
               confidence=max(probs.values()),
               probabilities=dict(probs), input_tokens=tokens, seconds=seconds)
    if question.qtype == "noul":
        a.noul = probs.get("true", 0.0)
    elif question.qtype == "choice":
        a.choice = max(probs, key=probs.get)
        opts = [k for k, _ in question.options()]
        if a.choice in opts:
            a.letter = LETTERS[opts.index(a.choice)]
    else:
        a.score = sum(int(k) * v for k, v in probs.items())
    return a


# ---------------------------------------------------------------------------
# 后端
# ---------------------------------------------------------------------------

class JevK5Prompt:
    """纯 prompt 侧：把 (state, question) 变成 (prompt, 选项 id)。无网络依赖，可单测。"""

    @staticmethod
    def render(state: str, criterion: str,
               options: Sequence[str]) -> str:
        return build_prompt(state, criterion, options)

    @staticmethod
    def options_for(question: Question) -> List[Tuple[str, str]]:
        return question.options()


class JevK5Client:
    """llama-server 上的 JevK5 读法（真 logprob）。

    用法::

        c = JevK5Client()                 # 默认 127.0.0.1:8090
        probs, tokens = c.read_letters(state, criterion, ["买", "不买"])
    """

    def __init__(self, url: str = DEFAULT_URL, temperature: float = None,
                 top_k: int = 40, timeout: float = 60.0,
                 size: str = "2b"):
        self.url = url.rstrip("/")
        self.top_k = top_k
        self.timeout = timeout
        self.temperature = (temperature if temperature is not None
                            else CALIBRATION_TEMPERATURE.get(size, 1.42))
        #: 有多少次提问有字母掉出 top-k（>0 说明 top_k 该调大）
        self.missing = 0

    # -- HTTP ----------------------------------------------------------

    def _request(self, path: str, payload: Optional[dict] = None) -> dict:
        """GET（payload 为 None）或 POST（payload 为 dict）。

        ⚠️ `HTTPError` 必须在 `URLError` **之前** catch —— 前者是后者的子类，
        顺序写反会把 404 报成"连不上"（实测踩过：llama-server 的 `/health` 只认 GET，
        POST 会 404，结果被报成连接失败）。
        """
        url = self.url + path
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise ModelUnavailable("%s → HTTP %s %s" % (url, e.code, body)) from e
        except urllib.error.URLError as e:
            raise ModelUnavailable("连不上 %s：%s" % (url, e)) from e
        except (ValueError, OSError) as e:
            raise ModelUnavailable("响应无法解析：%s" % e) from e

    def _post(self, path: str, payload: dict) -> dict:
        return self._request(path, payload)

    def health(self) -> bool:
        """`/health` 只认 GET（POST 会 404）。"""
        try:
            d = self._request("/health")
        except ModelUnavailable:
            return False
        return d.get("status") == "ok"

    # -- 读字母 --------------------------------------------------------

    def letter_logprobs(self, prompt: str) -> Tuple[Dict[str, float], int]:
        """一次前向：返回 {字母: logprob}（只含 top_k 里的）与 prompt token 数。"""
        # ⚠️ 必须先 tokenize：字符串直传会把 <|im_start|> 拆成普通字符
        toks = self._post("/tokenize", {
            "content": prompt, "add_special": False, "parse_special": True,
        }).get("tokens")
        if not toks:
            raise ModelUnavailable("tokenize 没返回 token")
        out = self._post("/completion", {
            "prompt": toks, "n_predict": 1, "n_probs": self.top_k,
            "temperature": 0, "cache_prompt": False,
        })
        try:
            top = out["completion_probabilities"][0]["top_logprobs"]
        except (KeyError, IndexError, TypeError) as e:
            raise ModelUnavailable(
                "服务没返回 completion_probabilities（该服务可能不支持 n_probs，"
                "LM Studio 自带 HTTP 服务就不支持，需要 llama-server）：%s" % e) from e
        return ({e["token"]: float(e["logprob"]) for e in top},
                int(out.get("tokens_evaluated", 0) or 0))

    def probabilities(self, state: str, question: Question
                      ) -> Tuple[Dict[str, float], int, float]:
        """一个类型化问题的校准概率分布。"""
        import time
        opts = question.options()
        truncated = 0
        if len(opts) > len(LETTERS):
            # 官方用 knockout 分趟；这里只保留前 16 个并明确标记，不静默丢
            truncated = len(opts) - len(LETTERS)
            opts = opts[:len(LETTERS)]
        texts = [t for _, t in opts]
        prompt = build_prompt(state, question.instructions, texts)
        t0 = time.time()
        seen, tokens = self.letter_logprobs(prompt)
        seconds = time.time() - t0
        floor = (min(seen.values()) - MISSING_MARGIN) if seen else -10.0
        lps = []
        missing = 0
        for i in range(len(texts)):
            if LETTERS[i] in seen:
                lps.append(seen[LETTERS[i]])
            else:
                lps.append(floor)
                missing += 1
        if missing:
            self.missing += 1
        top = max(lps)
        weights = [math.exp((z - top) / self.temperature) for z in lps]
        total = sum(weights)
        probs = {key: w / total for (key, _), w in zip(opts, weights)}
        if truncated:
            # ⚠️ 被截断的选项不能给 0 概率（会读成"绝不可能"）。官方用 knockout 分趟，
            # 这里做保守处理：给每个被丢弃的选项留 1/(N+1) 的份额，再从已有选项里
            # **按比例**扣掉这些份额 —— 这样剩下的选项仍然是合法分布，和仍为 1。
            share = 1.0 / (len(opts) + truncated + 1)
            keep = max(0.0, 1.0 - truncated * share)
            probs = {k: v * keep for k, v in probs.items()}
        return probs, tokens, seconds

    def decide(self, state: str, question: Question) -> Answer:
        probs, tokens, seconds = self.probabilities(state, question)
        a = answer_from_probs(question, probs, tokens, seconds)
        a.truncated = max(0, len(question.options()) - len(LETTERS))
        return a

    def decide_many(self, state: str,
                    questions: Sequence[Question]) -> List[Answer]:
        """按顺序问一串问题。服务端能批处理，但串行更简单也更好排查。"""
        return [self.decide(state, q) for q in questions]


# ---------------------------------------------------------------------------
# provider 抽象（为将来的云端兜底留位）
# ---------------------------------------------------------------------------

class Advisor:
    """模型顾问的统一接口：`decide(state, question) -> Answer`。"""

    name = "advisor"

    def available(self) -> bool:
        raise NotImplementedError

    def decide(self, state: str, question: Question) -> Answer:
        raise NotImplementedError

    def close(self) -> None:
        pass


class JevK5Advisor(Advisor):
    """本机 llama-server 上的 JevK5。"""

    name = "jevk5"

    def __init__(self, url: str = DEFAULT_URL, size: str = "2b",
                 timeout: float = 60.0):
        self.client = JevK5Client(url=url, size=size, timeout=timeout)

    def available(self) -> bool:
        return self.client.health()

    def decide(self, state: str, question: Question) -> Answer:
        return self.client.decide(state, question)
