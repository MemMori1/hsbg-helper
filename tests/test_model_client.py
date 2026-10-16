#!/usr/bin/env python3
"""M2：`model_client.OddsAdvisor` —— 用假 llm-odds 脚本离线测子进程契约。

策略：写一个假的 `llm-odds` 可执行文件（行为由环境变量控制），拿它当插件跑：

- `ask-many` 请求形状（items / options 裸文本 / params）从日志文件里回收断言；
- 结果映射（choice/noul/score → Answer、letter、truncated、missing）；
- 失败路径（全部失败 / 顶层错误 / 起不了 / 超时）→ ModelUnavailable。

真实插件联调不进单测（深挖见 `docs/实现笔记.md`）。
"""
import argparse
import contextlib
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hsbg.advisor import advisor_from_args                    # noqa: E402
from hsbg.model_client import OddsAdvisor, find_llm_odds      # noqa: E402
from hsbg.provider import (Advisor, Answer, JevK5Advisor,     # noqa: E402
                           ModelUnavailable, Question)

_FAKE_SRC = '''#!/usr/bin/python3
"""假 llm-odds：够真到能测契约，不碰网络。行为全靠环境变量。"""
import json, os, sys, time

args = sys.argv[1:]
log = os.environ.get("FAKE_ODDS_LOG")
data = sys.stdin.read() if args[:1] == ["ask-many"] else ""
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": args, "stdin": data,
                            "registry": os.environ.get("LLM_ODDS_REGISTRY", "")}) + "\\n")
if args[:1] == ["models"]:
    avail = os.environ.get("FAKE_ODDS_AVAILABLE", "0") == "1"
    print(json.dumps({"v": 1, "default": "m1", "default_available": avail}))
    sys.exit(0)

time.sleep(float(os.environ.get("FAKE_ODDS_SLEEP", "0")))
if os.environ.get("FAKE_ODDS_TOP_ERROR") == "1":
    print(json.dumps({"v": 1, "ok": False,
                      "error": {"code": "model_unavailable",
                                "message": "服务没起"}}))
    sys.exit(int(os.environ.get("FAKE_ODDS_RC", "3")))

req = json.loads(data)
trunc = int(os.environ.get("FAKE_ODDS_TRUNCATED", "0"))
missing = int(os.environ.get("FAKE_ODDS_MISSING", "0"))
results = []
for it in req.get("items") or []:
    task, iid = it.get("task"), it.get("id")
    if os.environ.get("FAKE_ODDS_FAIL_ID") == iid:
        results.append({"v": 1, "id": iid, "ok": False, "task": task,
                        "error": {"code": "model_unavailable", "message": "fake"}})
        continue
    ids = [o["id"] for o in it.get("options") or []]
    if task == "noul":
        probs = {"true": 0.75, "false": 0.25}
    elif task == "choice":
        probs = {ids[0]: 0.6}
        rest = 0.4 / max(1, len(ids) - 1)
        for oid in ids[1:]:
            probs[oid] = rest
    else:
        probs = {oid: 1.0 / max(1, len(ids)) for oid in ids}
    results.append({"v": 1, "id": iid, "ok": True, "task": task,
                    "probs": probs,
                    "metrics": {"elapsed_ms": 5.0, "tokens_evaluated": 11,
                                "missing_letters": missing, "truncated": trunc,
                                "cache": "off", "attempts": 1}})
print(json.dumps({"v": 1, "results": results}))
sys.exit(0)
'''

_FAKE_DIR = tempfile.TemporaryDirectory(prefix="hsbg-fake-odds-")
_FAKE = os.path.join(_FAKE_DIR.name, "fake-llm-odds")
with open(_FAKE, "w", encoding="utf-8") as _f:
    _f.write(_FAKE_SRC)
os.chmod(_FAKE, 0o755)


@contextlib.contextmanager
def _env(**env):
    """临时改环境变量（None = 删除），退出时恢复。"""
    old = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _read_log(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _log(name):
    return os.path.join(_FAKE_DIR.name, name)


def _questions():
    return [
        Question("choice", "Pick the best option.", ["alpha", "beta"],
                 key="pick_shop"),
        Question("noul", "Should I buy it?", key="buy_slot_1"),
        Question("score", "Rate the play.", ["bad", "ok", "good"],
                 key="rate_play"),
    ]


# ---------------------------------------------------------------------------
# 找插件
# ---------------------------------------------------------------------------

def test_find_llm_odds_env_priority():
    with _env(HSBG_ODDS_BIN=_FAKE):
        assert find_llm_odds() == _FAKE


def test_find_llm_odds_ignores_missing_env_path():
    bad = os.path.join(_FAKE_DIR.name, "nope")
    with _env(HSBG_ODDS_BIN=bad):
        got = find_llm_odds()
    assert got != bad  # 坏路径不该被采用（继续找 PATH / 兄弟目录）


# ---------------------------------------------------------------------------
# decide_many：请求形状与结果映射
# ---------------------------------------------------------------------------

def test_decide_many_maps_answers():
    with _env(FAKE_ODDS_LOG=_log("map.jsonl")):
        adv = OddsAdvisor(bin_path=_FAKE)
        out = adv.decide_many("EV", _questions())
    assert [a.key for a in out] == ["pick_shop", "buy_slot_1", "rate_play"]
    c, n, s = out
    assert c.probabilities == {"alpha": 0.6, "beta": 0.4}
    assert c.choice == "alpha" and c.letter == "A"
    assert c.input_tokens == 11 and abs(c.seconds - 0.005) < 1e-12
    assert n.noul == 0.75
    assert abs(s.score - 1.0) < 1e-9   # 均匀分布 0/1/2 → 期望 1.0
    assert adv.missing == 0


def test_request_payload_shape():
    log = _log("shape.jsonl")
    with _env(FAKE_ODDS_LOG=log):
        OddsAdvisor(bin_path=_FAKE).decide_many("EV TEXT", _questions())
    calls = _read_log(log)
    assert len(calls) == 1
    assert calls[0]["args"] == ["ask-many", "-"]
    req = json.loads(calls[0]["stdin"])
    items = req["items"]
    assert [i["task"] for i in items] == ["choice", "noul", "score"]
    assert items[0]["evidence"] == "EV TEXT"
    assert items[0]["criterion"] == "Pick the best option."
    # ⚠️ 选项是"裸文本"（不带前缀）—— 前缀由插件 option_prefix 渲染，
    #    两边拼出来的 prompt 才逐字节一致。
    assert items[0]["options"] == [{"id": "alpha", "text": "alpha"},
                                   {"id": "beta", "text": "beta"}]
    assert "options" not in items[1]        # noul 不传（插件有固定两项）
    assert items[2]["options"][0] == {"id": "0", "text": "bad"}
    assert "profile" not in req["params"]   # 没设档位就不下发
    assert req["params"]["timeout_s"] == 60.0


def test_profile_and_registry_via_env():
    log = _log("env.jsonl")
    with _env(FAKE_ODDS_LOG=log, HSBG_ODDS_PROFILE="jevk5-4b",
              HSBG_ODDS_REGISTRY="/x/models.json"):
        OddsAdvisor(bin_path=_FAKE).decide_many("EV", _questions()[:1])
    call = _read_log(log)[0]
    assert json.loads(call["stdin"])["params"]["profile"] == "jevk5-4b"
    assert call["registry"] == "/x/models.json"


def test_choice_dict_criteria_raw_options():
    # choice 字典形态（slot → 描述）也要走裸文本
    log = _log("dict.jsonl")
    q = Question("choice", "Which one?", {"s1": "Murloc", "s2": ""},
                 key="pick_shop")
    with _env(FAKE_ODDS_LOG=log):
        OddsAdvisor(bin_path=_FAKE).decide_many("EV", [q])
    items = json.loads(_read_log(log)[0]["stdin"])["items"]
    assert items[0]["options"] == [{"id": "s1", "text": "Murloc"},
                                   {"id": "s2", "text": "s2"}]  # 空描述退回 key


def test_decide_single():
    with _env(FAKE_ODDS_LOG=_log("single.jsonl")):
        a = OddsAdvisor(bin_path=_FAKE).decide(
            "EV", Question("noul", "?", key="k"))
    assert a.noul == 0.75


# ---------------------------------------------------------------------------
# 可用性 / 失败路径
# ---------------------------------------------------------------------------

def test_available_and_cache():
    with _env(FAKE_ODDS_AVAILABLE="1"):
        adv = OddsAdvisor(bin_path=_FAKE)
        assert adv.available() is True
        os.environ["FAKE_ODDS_AVAILABLE"] = "0"   # 30s 缓存期内不重探
        assert adv.available() is True
    with _env(FAKE_ODDS_AVAILABLE="0"):
        assert OddsAdvisor(bin_path=_FAKE).available() is False


def test_all_fail_raises():
    with _env(FAKE_ODDS_FAIL_ID="only"):
        adv = OddsAdvisor(bin_path=_FAKE)
        try:
            adv.decide_many("EV", [Question("noul", "?", key="only")])
        except ModelUnavailable as e:
            assert "only" in str(e) or "全部失败" in str(e)
            return
        raise AssertionError("全部失败应当抛 ModelUnavailable")


def test_partial_failure_keeps_success():
    with _env(FAKE_ODDS_FAIL_ID="pick_shop"):
        out = OddsAdvisor(bin_path=_FAKE).decide_many("EV", _questions())
    assert len(out) == 2
    assert [a.key for a in out] == ["buy_slot_1", "rate_play"]


def test_top_level_error_raises():
    with _env(FAKE_ODDS_TOP_ERROR="1"):
        adv = OddsAdvisor(bin_path=_FAKE)
        try:
            adv.decide_many("EV", _questions()[:1])
        except ModelUnavailable as e:
            assert "服务没起" in str(e)
            return
        raise AssertionError("顶层错误应当抛 ModelUnavailable")


def test_bin_missing_raises():
    adv = OddsAdvisor(bin_path=os.path.join(_FAKE_DIR.name, "definitely-nope"))
    try:
        adv.decide_many("EV", _questions()[:1])
    except ModelUnavailable as e:
        assert "起不了" in str(e)
        return
    raise AssertionError("不存在的插件应当抛 ModelUnavailable")


def test_timeout_kills_subprocess():
    with _env(FAKE_ODDS_SLEEP="5"):
        adv = OddsAdvisor(bin_path=_FAKE, timeout=0.3)
        try:
            adv.decide_many("EV", _questions()[:1])
        except ModelUnavailable as e:
            assert "超时" in str(e)
            return
        raise AssertionError("超时应当抛 ModelUnavailable")


def test_truncated_and_missing_propagate():
    with _env(FAKE_ODDS_TRUNCATED="2"):
        out = OddsAdvisor(bin_path=_FAKE).decide_many("EV", _questions()[:1])
    assert out[0].truncated == 2
    with _env(FAKE_ODDS_MISSING="1"):
        adv = OddsAdvisor(bin_path=_FAKE)
        adv.decide_many("EV", _questions())
    assert adv.missing == 3   # 三道题都有字母掉出 top-k


# ---------------------------------------------------------------------------
# 接线：advisor_from_args 优先插件；Advisor.decide_many 默认逐题
# ---------------------------------------------------------------------------

def test_advisor_from_args_prefers_plugin():
    args = argparse.Namespace(no_model=False, model_timeout=60.0,
                              model_size=None, model_url=None)
    with _env(HSBG_ODDS_BIN=_FAKE):
        adv = advisor_from_args(args)
    assert isinstance(adv, OddsAdvisor)
    assert adv.bin == _FAKE


def test_advisor_from_args_no_model():
    with _env(HSBG_ODDS_BIN=_FAKE):
        assert advisor_from_args(argparse.Namespace(no_model=True)) is None


def test_advisor_base_decide_many_is_serial():
    class Stub(Advisor):
        name = "stub"

        def available(self):
            return True

        def decide(self, state, q):
            return Answer(key=q.key, qtype=q.qtype, confidence=1.0,
                          noul=1.0 if q.qtype == "noul" else None)

    out = Stub().decide_many("e", [Question("noul", "?", key="a"),
                                   Question("noul", "?", key="b")])
    assert [x.key for x in out] == ["a", "b"]


def test_jevk5_advisor_decide_many_delegates():
    adv = JevK5Advisor()
    seen = []
    adv.client.decide_many = lambda state, qs: seen.append(state) or []
    assert adv.decide_many("EV", [Question("noul", "?")]) == []
    assert seen == ["EV"]
