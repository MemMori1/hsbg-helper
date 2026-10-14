#!/usr/bin/env python3
"""P4 模型顾问层的单元测试。

策略：**默认全部离线**——
- prompt 构造 / 答案映射 / 概率换算：纯函数，直接断言。
- HTTP 读法：用一个假的 `urlopen` 桩喂"服务端原始响应"，验证 tokenize→completion
  两步、缺 `completion_probabilities` 时的报错、以及 top_k 外字母的兜底。
- `advisor.consult()` 的降级：用一个永远不可用的 Advisor，断言规则部分照常产出。
- `build_evidence()`：断言产出**不含中文**（ADR-4 的硬约束），且包含商店/场面/对手。
- `model_server`：只测路径查找与命令拼装（真的起服务是集成测试的事）。
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hsbg import advisor as A                       # noqa: E402
from hsbg import provider as P                      # noqa: E402
from hsbg.cards import Card, CardDB, CardResolver   # noqa: E402
from hsbg.rules import RulesEngine, evaluate_state  # noqa: E402
from hsbg.state import Entity, GameState, Seat, ShopSlot   # noqa: E402


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _db():
    return CardDB(cards={
        "BG36_110": Card(card_id="BG36_110", name="Cheerful", type="MINION",
                         tech_level=1, attack=2, health=3, race="BEAST",
                         races=("BEAST",), mechanics=("BATTLECRY",),
                         pool_minion=True),
        "BG31_149": Card(card_id="BG31_149", name="Amplifier", type="MINION",
                         tech_level=2, attack=1, health=3, race="BEAST",
                         races=("BEAST",), mechanics=()),
        "BG28_504": Card(card_id="BG28_504", name="Sporeling", type="MINION",
                         tech_level=2, attack=2, health=2, race="BEAST",
                         races=("BEAST",), mechanics=("TAUNT",),
                         pool_minion=True),
        "BG20_HERO_101": Card(card_id="BG20_HERO_101", name="Zereila",
                              type="HERO", tech_level=None,
                              attack=None, health=None, race="",
                              races=(), mechanics=()),
        # 黑暗之赐的赐福（另一张卡）
        "BG_T_GIFT": Card(card_id="BG_T_GIFT", name="Harpy's Talons",
                          type="SPELL", tech_level=None, attack=None,
                          health=None, race="", races=(),
                          mechanics=("DIVINE_SHIELD", "WINDFURY"),
                          text="<b>Divine Shield</b>, <b>Windfury</b>"),
    })


def _state(with_shop=True, with_board=True):
    st = GameState(turn=9, step="MAIN_ACTION", local_player_id=6,
                   current_player_id=6)
    st.seats[6] = Seat(player_id=6, hero_entity_id=104,
                       hero_card_id="BG20_HERO_101", hero_name="Zereila",
                       is_local=True, health=30, max_health=30, damage=0,
                       tech_level=3, max_tech_level=6, resources=9,
                       resources_used=0)
    st.seats[1] = Seat(player_id=1, hero_entity_id=201, hero_card_id="BG20_HERO_101",
                       hero_name="Rival", is_local=False, health=12,
                       max_health=40, damage=28, tech_level=4)
    st.entities[104] = Entity(104, "BG20_HERO_101", "Zereila",
                              {"PLAYER_TECH_LEVEL": "3", "HEALTH": "30",
                               "DAMAGE": "0", "CONTROLLER": "6"})
    if with_shop:
        st.shop = [
            ShopSlot(slot_entity_id=900, minion_entity_id=901, card_id="BG36_110",
                     name="Cheerful", tech_level=1, attack=2, health=3,
                     belongs_to_local=True),
            ShopSlot(slot_entity_id=902, minion_entity_id=903, card_id="BG31_149",
                     name="Amplifier", tech_level=2, attack=1, health=3,
                     belongs_to_local=True),
            ShopSlot(slot_entity_id=904, minion_entity_id=None,
                     belongs_to_local=True),      # 空槽
        ]
    if with_board:
        st.board = [
            Entity(310, "BG28_504", "Sporeling",
                   {"ATK": "2", "HEALTH": "2", "ZONE": "PLAY", "CONTROLLER": "6",
                    "TECH_LEVEL": "2"}),
            Entity(311, "BG28_504", "Sporeling",
                   {"ATK": "2", "HEALTH": "2", "ZONE": "PLAY", "CONTROLLER": "6",
                    "TECH_LEVEL": "2"}),
            Entity(312, "BG31_149", "Amplifier",
                   {"ATK": "1", "HEALTH": "3", "ZONE": "PLAY", "CONTROLLER": "6",
                    "TECH_LEVEL": "2"}),
        ]
    return st


def _report(st):
    return RulesEngine(_db()).evaluate(st)


# ---------------------------------------------------------------------------
# prompt 构造
# ---------------------------------------------------------------------------

def test_letters_table_matches_official():
    assert P.LETTERS == "ABCDEFGHIJKLMNOP"
    assert P.MISSING_MARGIN == 2.0
    assert P.CALIBRATION_TEMPERATURE["2b"] == 1.42
    assert P.CALIBRATION_TEMPERATURE["9b"] == 1.089


def test_system_message_asks_for_letter_only():
    s = P.SYSTEM_MESSAGE.lower()
    assert "one listed option" in s
    assert "uppercase letter" in s
    assert "no explanation" in s


def test_prompt_prefills_empty_think_block():
    """thinking off = 预填空 think 块，答案位置的 token 才是选项字母。"""
    pr = P.build_prompt("EV", "CRIT", ["one", "two"])
    assert pr.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")
    assert pr.count("<think>") == 1 and pr.count("</think>") == 1
    assert "<|im_start|>system\n" in pr and "<|im_end|>" in pr


def test_payload_is_the_official_json_shape():
    import json
    body = json.loads(P.build_prompt("EVIDENCE", "CRIT", ["a", "b"]).split(
        "<|im_start|>user\n", 1)[1].split("<|im_end|>", 1)[0])
    assert body["evidence"] == "EVIDENCE"
    assert body["criterion"] == "CRIT"
    assert body["options"] == [{"letter": "A", "description": "a"},
                               {"letter": "B", "description": "b"}]


def test_payload_keeps_non_ascii_evidence_readable():
    pr = P.build_prompt("回合 9", "c", ["x"])
    assert "回合 9" in pr        # ensure_ascii=False：不转成 \u56de


def test_prompt_options_are_lettered_in_order():
    pr = P.build_prompt("e", "c", ["first", "second", "third"])
    assert '"letter": "C", "description": "third"' in pr


# ---------------------------------------------------------------------------
# 题型 → 选项
# ---------------------------------------------------------------------------

def test_noul_options_are_true_false():
    q = P.Question(qtype="noul", instructions="Is it true?")
    assert [k for k, _ in q.options()] == ["true", "false"]
    texts = [t for _, t in q.options()]
    assert texts[0].startswith("true: ") and texts[1].startswith("false: ")


def test_choice_options_use_criteria_text():
    q = P.Question(qtype="choice", instructions="pick", criteria=["甲", "乙"])
    assert [k for k, _ in q.options()] == ["甲", "乙"]
    assert [t for _, t in q.options()] == ["甲: 甲", "乙: 乙"]


def test_choice_options_accept_mapping():
    q = P.Question(qtype="choice", instructions="pick",
                   criteria={"A1": "the first one", "B1": ""})
    assert q.options() == [("A1", "A1: the first one"), ("B1", "B1: B1")]


def test_score_options_are_numbered_levels():
    q = P.Question(qtype="score", instructions="rate",
                   criteria=["bad", "ok", "good"])
    assert [k for k, _ in q.options()] == ["0", "1", "2"]
    assert q.options()[2][1] == "2: good"


# ---------------------------------------------------------------------------
# 概率 → 答案
# ---------------------------------------------------------------------------

def test_answer_noul_takes_p_true():
    q = P.Question(qtype="noul", instructions="x", key="k")
    a = P.answer_from_probs(q, {"true": 0.7, "false": 0.3})
    assert a.noul == 0.7 and a.confidence == 0.7 and a.describe() == "是"
    assert P.answer_from_probs(q, {"true": 0.2, "false": 0.8}).describe() == "否"


def test_answer_choice_picks_argmax_and_letter():
    q = P.Question(qtype="choice", instructions="x",
                   criteria=["one", "two", "three"], key="pick")
    a = P.answer_from_probs(q, {"one": 0.1, "two": 0.2, "three": 0.7})
    assert a.choice == "three" and a.letter == "C" and a.confidence == 0.7
    assert a.describe() == "three"


def test_answer_score_is_expected_level():
    q = P.Question(qtype="score", instructions="rate",
                   criteria=["bad", "ok", "good"])
    a = P.answer_from_probs(q, {"0": 0.0, "1": 0.25, "2": 0.75})
    assert abs(a.score - 1.75) < 1e-9
    assert a.describe() == "1.75"


def test_answer_rejects_empty_distribution():
    q = P.Question(qtype="noul", instructions="x")
    try:
        P.answer_from_probs(q, {})
    except ValueError:
        return
    raise AssertionError("空分布应当报 ValueError")


def test_answer_key_falls_back_to_instructions():
    q = P.Question(qtype="noul", instructions="Is this true?")
    assert P.answer_from_probs(q, {"true": 1.0}).key == "Is this true?"


# ---------------------------------------------------------------------------
# 概率换算（不碰网络：直接算）
# ---------------------------------------------------------------------------

def _softmax(lps, temperature):
    import math
    top = max(lps)
    w = [math.exp((z - top) / temperature) for z in lps]
    s = sum(w)
    return [x / s for x in w]


def test_softmax_with_calibration_temperature_is_softer_than_t0():
    """JevK5 的校准温度 > 1，会把极端分布拉平（这正是它置信度的来源）。"""
    sharp = _softmax([0.0, -8.0], 1.0)
    soft = _softmax([0.0, -8.0], 1.42)
    assert soft[0] < sharp[0]
    assert abs(sum(soft) - 1.0) < 1e-9


def test_missing_letter_falls_back_to_floor():
    """官方做法：字母不在 top_k 里时用 min(seen) - MISSING_MARGIN 兜底。"""
    seen = {"A": -0.5, "B": -1.5}
    floor = min(seen.values()) - P.MISSING_MARGIN
    assert floor == -3.5


# ---------------------------------------------------------------------------
# HTTP 读法（假 urlopen 桩）
# ---------------------------------------------------------------------------

class _FakeResp(object):
    def __init__(self, payload):
        self._b = payload.encode() if isinstance(payload, str) else payload

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake_urlopen(handler):
    """替换 `urllib.request.urlopen`；返回 (calls, restore)。"""
    import contextlib

    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.get_method(), req.full_url, req.data))
        return handler(req)

    saved = P.urllib.request.urlopen
    P.urllib.request.urlopen = fake_urlopen

    @contextlib.contextmanager
    def _restore():
        try:
            yield calls
        finally:
            P.urllib.request.urlopen = saved

    return calls, _restore


def test_health_uses_get_with_stub():
    import json

    def handler(req):
        return _FakeResp(json.dumps({"status": "ok"}))
    calls, restore = _install_fake_urlopen(handler)
    with restore():
        c = P.JevK5Client()
        assert c.health() is True
    assert calls and calls[0][0] == "GET"


def test_tokenize_then_completion_two_step_read():
    """⚠️ 必须先 /tokenize 再 /completion；字符串直传会把模板拆成普通字符。"""
    import json

    def handler(req):
        if req.full_url.endswith("/tokenize"):
            return _FakeResp(json.dumps({"tokens": [1, 2, 3]}))
        body = json.loads(req.data.decode())
        assert body["prompt"] == [1, 2, 3]      # 喂的是 token id 不是字符串
        assert body["n_predict"] == 1
        assert body["n_probs"] >= 16
        return _FakeResp(json.dumps({
            "tokens_evaluated": 42,
            "completion_probabilities": [{
                "top_logprobs": [{"token": "C", "logprob": -0.676},
                                 {"token": "A", "logprob": -1.136}],
            }],
        }))
    calls, restore = _install_fake_urlopen(handler)
    with restore():
        c = P.JevK5Client()
        seen, tokens = c.letter_logprobs("PROMPT")
    paths = [u.split("127.0.0.1:8090")[1] for _, u, _ in calls]
    assert paths == ["/tokenize", "/completion"], paths
    assert seen == {"C": -0.676, "A": -1.136}
    assert tokens == 42


def test_missing_completion_probabilities_raises_helpful_error():
    """LM Studio 自带服务不给 logprobs，报错必须指向 llama-server。"""
    import json

    def handler(req):
        if req.full_url.endswith("/tokenize"):
            return _FakeResp(json.dumps({"tokens": [1]}))
        return _FakeResp(json.dumps({"content": "A"}))    # 没有 completion_probabilities
    _, restore = _install_fake_urlopen(handler)
    with restore():
        c = P.JevK5Client()
        try:
            c.letter_logprobs("P")
        except P.ModelUnavailable as e:
            assert "llama-server" in str(e)
            return
        raise AssertionError("应当抛 ModelUnavailable")


def test_connection_failure_becomes_model_unavailable():
    def handler(req):
        raise P.urllib.error.URLError("connection refused")
    _, restore = _install_fake_urlopen(handler)
    with restore():
        c = P.JevK5Client()
        assert c.health() is False           # health() 自己吞掉异常
        try:
            c.letter_logprobs("P")
        except P.ModelUnavailable as e:
            assert "连不上" in str(e)
            return
        raise AssertionError("应当抛 ModelUnavailable")


def test_http_error_is_not_reported_as_connection_failure():
    """HTTPError 是 URLError 的子类：catch 顺序写反会把 404 报成"连不上"。"""
    def handler(req):
        raise P.urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
    _, restore = _install_fake_urlopen(handler)
    with restore():
        c = P.JevK5Client()
        try:
            c._request("/health")
        except P.ModelUnavailable as e:
            assert "HTTP 404" in str(e)
            assert "连不上" not in str(e)
            return
        raise AssertionError("应当抛 ModelUnavailable")


def test_more_than_sixteen_options_are_truncated_not_silently_dropped():
    import json

    def handler(req):
        if req.full_url.endswith("/tokenize"):
            return _FakeResp(json.dumps({"tokens": [1]}))
        return _FakeResp(json.dumps({
            "tokens_evaluated": 3,
            "completion_probabilities": [{
                "top_logprobs": [{"token": L, "logprob": -1.0 - i}
                                 for i, L in enumerate(P.LETTERS)],
            }],
        }))
    _, restore = _install_fake_urlopen(handler)
    with restore():
        c = P.JevK5Client()
        q = P.Question(qtype="choice", instructions="pick",
                       criteria=["c%d" % i for i in range(20)])
        probs, tokens, seconds = c.probabilities("e", q)
    assert len(probs) == 16            # 只留前 16 个，未静默丢（answer 会带 truncated）
    # 被丢弃选项的份额被按比例扣掉，剩下 16 项的和 < 1（缺失的正是 4 份份额），
    # 但仍是合法分布；关键是**没把被截断的选项当成概率 0**。
    share = 1.0 / (16 + 4 + 1)
    assert abs(sum(probs.values()) - (1.0 - 4 * share)) < 1e-9


def test_temperature_defaults_per_size():
    assert P.JevK5Client(size="2b").temperature == 1.42
    assert P.JevK5Client(size="9b").temperature == 1.089
    assert P.JevK5Client(size="whatever").temperature == 1.42      # 未知档位退回 2B


def test_default_url_points_at_llama_server():
    assert P.DEFAULT_URL == "http://127.0.0.1:8090"
    from hsbg import model_server as ms
    assert ms.DEFAULT_URL == P.DEFAULT_URL


def test_client_accepts_custom_url_and_strips_slash():
    c = P.JevK5Client(url="http://127.0.0.1:9999/")
    assert c.url == "http://127.0.0.1:9999"


# ---------------------------------------------------------------------------
# evidence：英文硬约束
# ---------------------------------------------------------------------------

def _has_cjk(s):
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def test_evidence_is_english_only():
    """ADR-4：JevK5 是 English-only，evidence 里绝不能出现中文。"""
    st = _state()
    ev = A.build_evidence(st, db=_db(), report=_report(st))
    assert not _has_cjk(ev), "evidence 里混进了中文：\n" + ev


def test_evidence_has_the_expected_sections():
    st = _state()
    ev = A.build_evidence(st, db=_db(), report=_report(st))
    assert "Hearthstone Battlegrounds, turn 9" in ev
    assert "My board (3 minions)" in ev
    assert "Shop (2 minions" in ev              # 空槽不计
    assert "Opponents' health:" in ev
    assert "player 1 12" in ev                 # effective_health = 40 - 28
    assert "Golden " not in ev


def test_evidence_uses_card_db_english_names_not_log_names():
    st = _state()
    ev = A.build_evidence(st, db=_db(), report=_report(st))
    assert "Sporeling" in ev and "Amplifier" in ev
    assert "Cheerful" in ev


def test_evidence_without_card_db_still_renders():
    st = _state()
    ev = A.build_evidence(st, db=None, report=_report(st))
    assert "Hearthstone Battlegrounds" in ev
    assert "Sporeling" in ev            # 退回实体自带名


def test_evidence_marks_non_recruit_phase():
    st = _state()
    st.step = "MAIN_COMBAT"
    ev = A.build_evidence(st, db=_db(), report=_report(st))
    assert "not the recruit phase" in ev


def test_evidence_includes_next_upgrade_cost_when_known():
    st = _state()
    ev = A.build_evidence(st, db=_db(), report=_report(st))
    assert "Next tavern upgrade costs" in ev


# ---------------------------------------------------------------------------
# 提问构造
# ---------------------------------------------------------------------------

def test_pick_question_options_are_english_and_indexed():
    st = _state()
    rep = _report(st)
    q = A.pick_question(st, rep)
    assert q is not None and q.qtype == "choice"
    # ⚠️ 选项里的槽位号是**盘面显示序号**（1..N），且顺序必须与 evidence 的
    # `Shop (N)` 一致 —— 都按 `slot_entity_id` 升序（商店从左到右）。
    # 这里同时锁住"不是按分数排的 report.shop 顺序"这一点：如果谁改回
    # `rep.shop` 顺序，选项号就会和 evidence 第 N 行错位（实测踩过）。
    ordered = sorted(rep.shop, key=lambda it: it.slot_entity_id)
    assert q.criteria[:-1] == ["Buy the minion in shop slot %d" % i
                               for i in range(1, len(ordered) + 1)]
    assert q.criteria[-1] == "Buy nothing, keep my gold"
    for c in q.criteria:
        assert not _has_cjk(c)


def test_pick_question_needs_two_slots():
    st = _state()
    st.shop = st.shop[:1]
    rep = evaluate_state(st, _db())
    assert A.pick_question(st, rep) is None


def test_level_question_mentions_cost_and_affordability():
    st = _state()
    rep = _report(st)
    q = A.level_question(st, rep)
    assert q is not None and q.qtype == "noul" and q.key == "level_up"
    assert "Upgrading my tavern from tier" in q.instructions
    assert not _has_cjk(q.instructions)


def test_buy_questions_cover_every_shop_item():
    st = _state()
    rep = _report(st)
    qs = A.buy_questions(st, rep)
    # key 用槽位实体 id（唯一），不用只在一帧内唯一的显示序号
    assert [q.key for q in qs] == ["buy_slot_%d" % it.slot_entity_id
                                   for it in rep.shop]
    assert all(q.qtype == "noul" for q in qs)
    assert all(not _has_cjk(q.instructions) for q in qs)


def test_build_questions_order_is_pick_level_buys():
    st = _state()
    rep = _report(st)
    qs = A.build_questions(st, rep)
    assert qs[0].key == "pick_shop"
    assert qs[1].key == "level_up"
    assert all(q.key.startswith("buy_slot_") for q in qs[2:])


# ---------------------------------------------------------------------------
# consult 的降级
# ---------------------------------------------------------------------------

class _DeadAdvisor(P.Advisor):
    name = "dead"

    def available(self):
        return False

    def decide(self, state, question):
        raise AssertionError("不可用时不该被调用")


class _BrokenAdvisor(P.Advisor):
    name = "broken"

    def available(self):
        return True

    def decide(self, state, question):
        raise P.ModelUnavailable("boom")


class _FakeAdvisor(P.Advisor):
    name = "fake"

    def __init__(self):
        self.seen = []

    def available(self):
        return True

    def decide(self, state, question):
        self.seen.append((state, question))
        if question.qtype == "choice":
            probs = {k: 0.1 for k, _ in question.options()}
            probs[question.options()[0][0]] = 0.9
        else:
            probs = {"true": 0.8, "false": 0.2}
        return P.answer_from_probs(question, probs, tokens=10, seconds=0.5)


def test_consult_rules_only_always_produces_report():
    st = _state()
    adv = A.consult(st, db=_db(), advisor=None)
    assert adv.report is not None
    assert adv.provider == "rules-only"
    assert adv.used_model is False
    assert "Hearthstone Battlegrounds" in adv.evidence


def test_consult_falls_back_when_advisor_unavailable():
    st = _state()
    adv = A.consult(st, db=_db(), advisor=_DeadAdvisor())
    assert adv.used_model is False
    assert "不可用" in adv.note
    assert adv.report.shop            # 规则部分照常有


def test_consult_falls_back_when_advisor_raises():
    st = _state()
    adv = A.consult(st, db=_db(), advisor=_BrokenAdvisor())
    assert adv.used_model is False
    assert "boom" in adv.note
    assert adv.report.shop


def test_consult_with_working_advisor_returns_all_answers():
    st = _state()
    fake = _FakeAdvisor()
    adv = A.consult(st, db=_db(), advisor=fake)
    assert adv.provider == "fake"
    assert adv.used_model is True
    keys = [a.key for a in adv.answers]
    assert "pick_shop" in keys and "level_up" in keys
    assert any(k.startswith("buy_slot_") for k in keys)
    assert adv.answer_for("level_up").noul == 0.8
    assert adv.answer_for("pick_shop").letter == "A"


def test_consult_passes_the_same_evidence_to_every_question():
    st = _state()
    fake = _FakeAdvisor()
    A.consult(st, db=_db(), advisor=fake)
    evidences = {e for e, _ in fake.seen}
    assert len(evidences) == 1
    assert not _has_cjk(evidences.pop())


def test_consult_infers_card_db_from_resolver():
    """只传 resolver 时也要带上卡表，否则 evidence 退化成裸 CardID。

    ⚠️ 这是 `panel.py` 的调用形态（`consult(snapshot, resolver=..., advisor=...)`）。
    漏掉的话模型看到的是 `BG36_506 (tier 4, 5/2)` 而不是 `复映无人机 (...)`，
    实测会导致同一帧问出**另一个答案**（带卡表答 slot 2，不带卡表答 slot 1）。
    """
    st = _state()
    resolver = CardResolver(db=_db())
    with_db = A.consult(st, db=_db(), advisor=None)
    via_resolver = A.consult(st, resolver=resolver, advisor=None)
    assert via_resolver.evidence == with_db.evidence
    assert "BG36_506" not in via_resolver.evidence


def test_consult_without_db_or_resolver_still_works():
    st = _state()
    adv = A.consult(st, advisor=None)
    assert adv.report is not None
    assert "Hearthstone Battlegrounds" in adv.evidence


def test_pick_shop_text_uses_display_order_not_score_order():
    """`pick_shop` 的中文翻译必须按槽位顺序查，不能按 `report.shop` 的分数顺序。

    ⚠️ 实测踩过：`report.shop` 是按分数降序的，直接 `report.shop[num-1]`
    会把"商店第 1 项"翻成排第一的那张（无面行动者），而模型说的其实是
    槽位 1（蓝色扩音机）。
    """
    st = _state()
    rep = _report(st)
    ordered = sorted(rep.shop, key=lambda it: it.slot_entity_id)
    assert ordered[0].name != rep.shop[0].name, "夹具要让两种顺序不同才有意义"
    ans = P.answer_from_probs(
        P.Question(qtype="choice", instructions="pick",
                   criteria=["Buy the minion in shop slot 1",
                             "Buy nothing, keep my gold"],
                   key="pick_shop"),
        {"Buy the minion in shop slot 1": 0.9,
         "Buy nothing, keep my gold": 0.1})
    text = A.answer_text("pick_shop", ans, rep)
    assert ordered[0].name in text
    assert rep.shop[0].name not in text


def test_buy_predictions_map_slot_entity_id_to_probability():
    st = _state()
    adv = A.consult(st, db=_db(), advisor=_FakeAdvisor())
    preds = adv.buy_predictions()
    assert preds
    report = adv.report
    assert set(preds) <= {it.slot_entity_id for it in report.shop}
    assert all(0.0 <= v <= 1.0 for v in preds.values())


def test_slot_label_speaks_in_display_order_not_slot_ids():
    """⚠️ `ShopItem.index` 是游戏内槽位号（刷新会越来越大），不是盘面的第几项。"""
    st = _state()
    rep = _report(st)
    first_slot = rep.shop[0].slot_entity_id
    label = A.slot_label(st.shop, first_slot, rep)
    assert rep.shop[0].name in label
    assert "商店第" in label
    # 未知槽位也不能崩
    assert A.slot_label(st.shop, 999999, rep)


def test_advisor_from_args_respects_no_model():
    args = types.SimpleNamespace(no_model=True)
    assert A.advisor_from_args(args) is None
    args = types.SimpleNamespace(no_model=False, model_url=None,
                                 model_size="2b", model_timeout=30.0)
    a = A.advisor_from_args(args)
    assert a is not None and a.name == "jevk5"
    assert a.client.url == P.DEFAULT_URL
    assert a.client.timeout == 30.0


# ---------------------------------------------------------------------------
# model_server：查找与命令拼装（不起服务）
# ---------------------------------------------------------------------------

def test_serve_command_quotes_paths():
    from hsbg import model_server as ms
    cmd = ms.serve_command(server="/tmp/my llama/llama-server",
                           model="/tmp/Jev K5.gguf", port=8091, context=4096)
    assert cmd.startswith('"/tmp/my llama/llama-server"')
    assert '-m "/tmp/Jev K5.gguf"' in cmd
    assert "-c 4096" in cmd
    assert "--port 8091" in cmd
    assert "--host 127.0.0.1" in cmd


def test_find_llama_server_prefers_highest_version(tmpdir=None):
    import tempfile
    from hsbg import model_server as ms
    with tempfile.TemporaryDirectory() as d:
        for ver in ("1.7.1", "2.55.0", "2.56.0"):
            sub = os.path.join(d, "llama.cpp-mac-arm64-apple-metal-advsimd-" + ver)
            os.makedirs(sub)
            p = os.path.join(sub, "llama-server")
            with open(p, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(p, 0o755)
        got = ms.find_llama_server(os.path.join(d, "*", "llama-server"))
        assert got is not None and got.endswith("2.56.0/llama-server")


def test_find_gguf_prefers_best_quantization():
    import tempfile
    from hsbg import model_server as ms
    with tempfile.TemporaryDirectory() as d:
        for name in ("jevk5-2b-v0.2-Q4_K_M.gguf", "jevk5-2b-v0.2-Q8_0.gguf",
                     "jevk5-2b-v0.2-Q5_K_M.gguf"):
            with open(os.path.join(d, name), "wb") as f:
                f.write(b"GGUF" + name.encode())
        got = ms.find_gguf(os.path.join(d, "*.gguf"))
        assert got is not None and "Q8_0" in got


def test_find_gguf_prefers_4b_over_2b_even_with_worse_quantization():
    """参数量档位优先于量化档位 —— 4B Q4_K_M 要赢过 2B Q8_0。

    这不是笔误：JevBench v1.4 上 9B 反而不如 4B，而且 2B 的区分度实测明显不足
    （P(买) 全挤在 0.42~0.48）。所以"量化更好"不等于"更该用"。
    """
    import tempfile
    from hsbg import model_server as ms
    with tempfile.TemporaryDirectory() as d:
        for name in ("jevk5-2b-v0.2-Q8_0.gguf", "jevk5-4b-v0.2-Q4_K_M.gguf"):
            with open(os.path.join(d, name), "wb") as f:
                f.write(b"GGUF" + name.encode())
        got = ms.find_gguf(os.path.join(d, "*.gguf"))
        assert got is not None and "4b" in os.path.basename(got), got


def test_find_gguf_prefers_2b_over_9b_when_no_4b_present():
    """没有 4B 时选 2B 而不是 9B（9B 在公开评测上更差且校准更糟）。"""
    import tempfile
    from hsbg import model_server as ms
    with tempfile.TemporaryDirectory() as d:
        for name in ("jevk5-9b-Q5_K_M.gguf", "jevk5-2b-v0.2-Q4_K_M.gguf"):
            with open(os.path.join(d, name), "wb") as f:
                f.write(b"GGUF" + name.encode())
        got = ms.find_gguf(os.path.join(d, "*.gguf"))
        assert got is not None and "2b" in os.path.basename(got), got


def test_size_key_reads_parameter_count_and_version():
    from hsbg import model_server as ms
    assert ms.size_key("/x/jevk5-4b-v0.2-Q4_K_M.gguf") == "4b-v0.2"
    assert ms.size_key("/x/jevk5-2b-v0.2-Q8_0.gguf") == "2b-v0.2"
    assert ms.size_key("/x/jevk5-9b-Q5_K_M.gguf") == "9b"
    # `jevk5` 里的那个 5 不能被当成参数量
    assert ms.size_key("/x/jevk5-unknown.gguf") == ""


def test_4b_v02_gets_its_own_calibration_temperature():
    """4B 的 v0.2 与 v0.3 校准温度不同（1.532 vs 1.367），认错会让概率偏平。"""
    from hsbg.provider import CALIBRATION_TEMPERATURE, resolve_temperature_key
    assert resolve_temperature_key("4b-v0.2") == "4b-v0.2"
    assert CALIBRATION_TEMPERATURE["4b-v0.2"] == 1.532
    assert resolve_temperature_key("4b-v0.3") == "4b"
    # 表里没有 `2b-v0.2` 这条，要退到 `2b`
    assert resolve_temperature_key("2b-v0.2") == "2b"
    assert resolve_temperature_key("") == ""


def test_client_temperature_follows_the_size_key():
    from hsbg.provider import JevK5Client
    assert JevK5Client(size="4b-v0.2").temperature == 1.532
    assert JevK5Client(size="4b").temperature == 1.367
    assert JevK5Client(size="2b").temperature == 1.42
    # 显式 temperature 仍然优先
    assert JevK5Client(size="4b-v0.2", temperature=2.0).temperature == 2.0


def test_serve_command_has_placeholders_when_nothing_found():
    from hsbg import model_server as ms
    cmd = ms.serve_command(server="S", model="M")
    assert '"S"' in cmd and '"M"' in cmd


# ---------------------------------------------------------------------------
# 待选（饰品四选一 / 黑赐三选一）进 evidence 与提问
# ---------------------------------------------------------------------------

def _pending_state(kind="trinket", source="BG30_Trinket_1st",
                   name="Lesser Trinket"):
    """一个"正在等玩家做四选一/三选一"的盘面。"""
    from hsbg.state import ChoiceOption as CO
    from hsbg.state import PendingChoice
    st = _state()
    pc = PendingChoice(choice_id=3, kind="GENERAL", source_card_id=source,
                       source_name=name, player="me", is_local=True)
    pc.options.append(CO(index=0, entity_id=3001, card_id="BG36_110",
                         name="log0", zone="SETASIDE"))
    pc.options.append(CO(index=1, entity_id=3002, card_id="BG31_149",
                         name="log1", zone="SETASIDE"))
    pc.options.append(CO(index=2, entity_id=3003, card_id="BG28_504",
                         name="log2", zone="SETASIDE"))
    st.pending_choice = pc
    return st


def test_evidence_lists_the_pending_choice_options():
    """⚠️ 不给模型看选项就让它推概率 = 瞎猜。"""
    st = _pending_state()
    ev = A.build_evidence(st, db=_db(), report=_report(st))
    assert "choose exactly one" in ev
    assert "Cheerful" in ev and "Amplifier" in ev and "Sporeling" in ev
    assert not _has_cjk(ev), "evidence 里混进了中文：\n" + ev


def test_evidence_without_choice_has_no_choice_section():
    st = _state()
    ev = A.build_evidence(st, db=_db(), report=_report(st))
    assert "choose exactly one" not in ev


def test_evidence_shows_the_dark_gift_of_each_option():
    """⚠️ 黑赐三选一的选项是随从，但强弱取决于**赐福**给了什么词条。

    实测同一只随从可配不同赐福（`鹰身人之爪` 给 圣盾+风怒、
    `防御献祭` 给 +10 血+亡语），不把赐福写进 evidence 等于让模型瞎猜。
    """
    st = _pending_state(kind="dark_gift", source="BG36_MidGameEffect_010",
                        name="Battlegrounds Dark Gift")
    st.pending_choice.options[0].gift_card_id = "BG_T_GIFT"
    ev = A.build_evidence(st, db=_db(), report=_report(st))
    assert "Dark Gift: Harpy's Talons" in ev
    assert "Divine Shield" in ev and "Windfury" in ev
    assert not _has_cjk(ev), "evidence 里混进了中文：\n" + ev


def test_dark_gift_question_options_mention_the_gift():
    """`choice` 题的选项文本也要带赐福，否则模型只看到随从名。"""
    st = _pending_state(kind="dark_gift", source="BG36_MidGameEffect_010",
                        name="Battlegrounds Dark Gift")
    st.pending_choice.options[0].gift_card_id = "BG_T_GIFT"
    rep = _report(st)
    q = A.choice_question(st, rep)
    assert q is not None and q.qtype == "choice"
    joined = " | ".join(str(c) for c in q.criteria)
    assert "Harpy's Talons" in joined
    assert not _has_cjk(joined), "题面里混进了中文：\n" + joined


def test_evidence_lists_equipped_trinkets():
    """已装备饰品决定打法方向，必须进 evidence。"""
    from hsbg.state import Trinket
    st = _state()
    st.trinkets = [Trinket(size="small", card_id="BG36_110", name="log",
                           entity_id=300)]
    ev = A.build_evidence(st, db=_db(), report=_report(st))
    assert "My trinkets:" in ev
    assert "Small Trinket" in ev
    assert not _has_cjk(ev)


def test_evidence_reports_triple_progress():
    """三连进度要写成 `2/3` 而不是 `2/1`（needed 是"还差几张"）。"""
    st = _state()
    ev = A.build_evidence(st, db=_db(), report=_report(st))
    assert "Triple progress" in ev
    for line in ev.splitlines():
        if line.startswith("Triple progress"):
            frac = line.split()[2]
            got, total = frac.split("/")
            assert total == "3", "分母必须是 3，实测写成 needed 会得到 2/1: " + line
            assert int(got) <= 3
            break


def test_evidence_drops_internal_mechanic_placeholders():
    """`TRIGGER_VISUAL` 占全库 24%，是内部占位符，绝不能喂给模型。"""
    from hsbg.cards import Card
    db = CardDB(cards={
        "BG36_110": Card(card_id="BG36_110", name="Cheerful", type="MINION",
                         tech_level=1, attack=2, health=3, race="BEAST",
                         races=("BEAST",),
                         mechanics=("TRIGGER_VISUAL", "DIVINE_SHIELD"),
                         pool_minion=True),
    })
    st = _state()
    ev = A.build_evidence(st, db=db, report=_report(st))
    assert "TRIGGER_VISUAL" not in ev
    assert "DIVINE_SHIELD" in ev


def test_choice_question_maps_every_option_to_its_number():
    st = _pending_state()
    rep = _report(st)
    q = A.choice_question(st, rep)
    assert q is not None
    assert q.key == "pending_choice" and q.qtype == "choice"
    # ⚠️ `pick_question` 立的契约是"裸文本即选项 id"，所以 `choice_question`
    # 靠 **"N. " 数字前缀**把显示序号带出去；翻译层再按这个数字换回中文卡名。
    # 千万别改成 `(id, 文本)` 元组 —— 那会同时破坏 `pick_shop` 的既有契约。
    assert [c.split(".")[0] for c in q.criteria] == ["1", "2", "3"]
    assert q.criteria[0].startswith("1. ") and "Cheerful" in q.criteria[0]
    assert "Lesser Trinket" in q.instructions


def test_choice_question_absent_without_pending_choice():
    st = _state()
    assert A.choice_question(st, _report(st)) is None


def test_pending_choice_is_asked_first():
    """待选是当前唯一会卡住回合的决策，必须排在最前。"""
    st = _pending_state()
    qs = A.build_questions(st, _report(st))
    assert qs and qs[0].key == "pending_choice"


def test_pending_choice_answer_translates_to_chinese_card_name():
    from hsbg.provider import Answer
    st = _pending_state()
    rep = _report(st)
    # 模型返回的就是我们喂进去的选项原文（带 "N. " 前缀）
    a = Answer(key="pending_choice", qtype="choice", confidence=0.6,
               choice="2. Amplifier", probabilities={"2. Amplifier": 0.6})
    # 前缀 "2" → 第 2 项的中文标签（夹具卡表名 Amplifier）
    assert A.answer_text("pending_choice", a, rep) == "Amplifier"


def test_pending_choice_text_falls_back_to_english_text():
    """模型偶尔回整段选项文本，按前缀反查，别把英文原样丢给用户。"""
    from hsbg.provider import Answer
    st = _pending_state()
    rep = _report(st)
    a = Answer(key="pending_choice", qtype="choice", confidence=0.6,
               choice="Amplifier", probabilities={"Amplifier": 0.6})
    got = A.answer_text("pending_choice", a, rep)
    assert got and not got.startswith("Amplifier ")


def test_choice_ranking_prefers_probabilities_and_has_labels():
    from hsbg.provider import Answer
    st = _pending_state()
    rep = _report(st)
    a = Answer(key="pending_choice", qtype="choice", confidence=0.6,
               choice="1",
               probabilities={"Cheerful": 0.1, "Amplifier": 0.7,
                              "Sporeling": 0.2})
    rank = A.choice_ranking(rep, a)
    assert rank[0][1] == 0.7
    assert len(rank) == 3
    assert all(nm and "选项 " not in nm for nm, _ in rank), rank


def test_consult_asks_the_pending_choice():
    st = _pending_state()
    fake = _FakeAdvisor()
    A.consult(st, db=_db(), advisor=fake)
    keys = [q.key for _, q in fake.seen]
    assert "pending_choice" in keys


def test_question_text_knows_pending_choice():
    assert A.question_text("pending_choice") == "这个四选一/三选一该选哪个"


def test_panel_frame_carries_choice_probabilities():
    """面板要能在每个选项旁显示模型概率（规则层对饰品不打分）。"""
    from hsbg import panel as PN
    from hsbg.provider import Answer
    st = _pending_state()
    rep = _report(st)
    adv = A.Advice(report=rep)
    adv.provider = "fake"
    adv.answers = [Answer(key="pending_choice", qtype="choice", confidence=0.6,
                          choice="2. Amplifier",
                          probabilities={"1. Cheerful": 0.1,
                                         "2. Amplifier": 0.7,
                                         "3. Sporeling": 0.2})]
    frame = PN.build_frame(st, resolver=CardResolver(_db()), report=rep,
                           advice=adv)
    pc = frame.to_dict().get("pending_choice")
    assert pc is not None
    probs = {o["name"]: o["probability"] for o in pc["options"]}
    assert probs["Amplifier"] == 0.7
    assert probs["Cheerful"] == 0.1
    labels = [a["question"] for a in frame.to_dict()["model_answers"]]
    assert "这个四选一/三选一该选哪个" in labels


def test_panel_choice_probabilities_absent_without_model():
    from hsbg import panel as PN
    st = _pending_state()
    rep = _report(st)
    frame = PN.build_frame(st, resolver=CardResolver(_db()), report=rep,
                           advice=None)
    pc = frame.to_dict().get("pending_choice")
    assert pc is not None
    assert all(o["probability"] is None for o in pc["options"])
