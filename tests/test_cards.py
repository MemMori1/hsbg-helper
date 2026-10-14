"""P2 卡牌数据层的单元测试（零依赖）。

⚠️ 故意**不**依赖 `data/cards_zhCN.json`：那是个 1.2 MB 的生成物，测试要能在
干净检出上跑。这里用一份 4 张卡的手搓缓存，字段与真实缓存完全一致。
"""

import json
import tempfile
from pathlib import Path

from hsbg.cards import (
    Card,
    CardDB,
    CardResolver,
    build_cache,
    slugify_card_id,
)
from hsbg.names import collect_names_from_events, load_names, save_names


def _fixture_cache() -> dict:
    raw = [
        # 战棋随从（带 techLevel / race / mechanics）
        {
            "id": "BG36_110", "name": "欢悦", "set": "BATTLEGROUNDS", "type": "MINION",
            "techLevel": 1, "attack": 2, "health": 3, "race": "ABERRATION",
            "races": ["ABERRATION"], "mechanics": ["BATTLECRY"],
            "isBattlegroundsPoolMinion": True, "dbfId": 1,
        },
        # 战棋随从：ALL 种族（对任何种族都算）
        {
            "id": "BG_AMALGAM", "name": "融合怪", "set": "BATTLEGROUNDS", "type": "MINION",
            "techLevel": 3, "attack": 3, "health": 4, "race": "ALL",
            "races": ["ALL"], "isBattlegroundsPoolMinion": True, "dbfId": 2,
        },
        # 战棋英雄（不是随从，没有 techLevel）
        {
            "id": "TB_BaconShop_HERO_91", "name": "时光扭曲者克罗米",
            "set": "BATTLEGROUNDS", "type": "HERO", "dbfId": 3,
        },
        # 非战棋卡，必须被裁掉
        {
            "id": "CS2_001", "name": "并不在战棋里", "set": "CORE", "type": "MINION",
            "dbfId": 4,
        },
    ]
    return build_cache(raw)


def _write(tmp: Path, name: str, payload: dict) -> Path:
    p = tmp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_build_cache_keeps_only_battlegrounds_cards():
    cache = _fixture_cache()
    assert set(cache["cards"]) == {"BG36_110", "BG_AMALGAM", "TB_BaconShop_HERO_91"}
    assert cache["count"] == 3
    # 空字段要被丢掉，避免 1.2 MB 变 3 MB
    assert "races" not in cache["cards"]["TB_BaconShop_HERO_91"]


def test_build_cache_drops_none_and_empty_fields():
    cache = _fixture_cache()
    hero = cache["cards"]["TB_BaconShop_HERO_91"]
    assert hero == {"id": "TB_BaconShop_HERO_91", "name": "时光扭曲者克罗米",
                    "type": "HERO", "dbfId": 3} or "techLevel" not in hero
    assert all(v not in (None, "", [], {}) for v in cache["cards"]["BG36_110"].values())


def test_card_from_dict_roundtrip():
    c = Card.from_dict({
        "id": "BG36_110", "name": "欢悦", "type": "MINION", "techLevel": 1,
        "attack": 2, "health": 3, "race": "ABERRATION", "races": ["ABERRATION"],
        "mechanics": ["BATTLECRY"], "isBattlegroundsPoolMinion": True,
    })
    assert c.card_id == "BG36_110"
    assert c.is_minion and not c.is_hero
    assert c.tech_level == 1 and c.tech_level_text == "1星"
    assert c.describe() == "欢悦 1星 2/3 ABERRATION"
    again = Card.from_dict(c.to_dict())
    assert again == c


def test_card_display_name_never_empty():
    c = Card.from_dict({"id": "BG36_110"})
    assert c.display_name == "BG36_110"
    assert Card.from_dict({"id": ""}).describe() == ""


def test_card_db_load_and_lookup():
    with tempfile.TemporaryDirectory() as d:
        path = _write(Path(d), "cards.json", _fixture_cache())
        db = CardDB.load(path)
        assert len(db) == 3
        assert "BG36_110" in db
        assert db.get("CS2_001") is None            # 非战棋卡没进缓存
        assert db.name_of("BG36_110") == "欢悦"
        # ⚠️ 查不到必须原样返回 CardID，不能返回空串（展示层会变成一片空白）
        assert db.name_of("CS2_001") == "CS2_001"
        assert db.name_of(None) == ""


def test_card_db_load_or_empty_degrades_on_missing_file():
    db = CardDB.load_or_empty(Path("/nonexistent/cards.json"))
    assert len(db) == 0
    assert db.name_of("BG36_110") == "BG36_110"


def test_pool_minions_and_tech_levels():
    with tempfile.TemporaryDirectory() as d:
        db = CardDB.load(_write(Path(d), "cards.json", _fixture_cache()))
        assert sorted(c.card_id for c in db.pool_minions()) == ["BG36_110", "BG_AMALGAM"]
        assert sorted(c.card_id for c in db.minions()) == ["BG36_110", "BG_AMALGAM"]
        assert db.tech_levels() == [1, 3]
        assert [c.name for c in db.by_tech_level(1)] == ["欢悦"]
        assert db.stats()["total"] == 3


def test_by_race_includes_all_race_minions():
    with tempfile.TemporaryDirectory() as d:
        db = CardDB.load(_write(Path(d), "cards.json", _fixture_cache()))
        # `ALL` 种族的融合怪对任何种族查询都要被带上（游戏里就是这么算的）
        ids = sorted(c.card_id for c in db.by_race("BEAST"))
        assert ids == ["BG_AMALGAM"]
        assert [c.card_id for c in db.by_race("ABERRATION")] == ["BG36_110", "BG_AMALGAM"]
        assert db.races() == ["ABERRATION", "ALL"]


def test_verify_against_log_names_reports_mismatch_and_missing():
    with tempfile.TemporaryDirectory() as d:
        db = CardDB.load(_write(Path(d), "cards.json", _fixture_cache()))
        rep = db.verify_against_log_names({
            "BG36_110": "欢悦",            # 一致
            "TB_BaconShop_HERO_91": "克罗米",  # 短名，卡表是全名 → 不一致
            "EDR_100t13e": "某张非战棋卡",     # 卡表里没有 → missing
        })
        assert rep["checked"] == 3
        assert rep["ok"] == 1
        assert rep["mismatch"] == 1
        assert rep["missing"] == 1
        assert rep["samples"][0]["card_id"] == "TB_BaconShop_HERO_91"


def test_slugify_card_id():
    assert slugify_card_id("TB_BaconShop_HP_038e") == "TB_BaconShop_HP_038e"
    assert slugify_card_id("标记交换玩家附加效果[DNT]") == "标记交换玩家附加效果_DNT"
    assert slugify_card_id("") == "UNKNOWN"
    assert slugify_card_id("[]") == "UNKNOWN"


def test_resolver_prefers_log_short_name_over_card_db():
    """日志短名优先——实测 6 条英雄皮肤对不上（洛卡拉 vs 奥术战士洛卡拉）。"""
    with tempfile.TemporaryDirectory() as d:
        db = CardDB.load(_write(Path(d), "cards.json", _fixture_cache()))
        r = CardResolver(db=db, log_names={"TB_BaconShop_HERO_91": "克罗米"})
        assert r.name_of("TB_BaconShop_HERO_91") == "克罗米"
        assert r.name_of("BG36_110") == "欢悦"        # 没有覆盖就用卡表
        assert r.name_of("UNKNOWN_X") == "UNKNOWN_X"  # 都没有就退回 CardID


def test_resolver_describe_appends_stats_only_for_minions():
    with tempfile.TemporaryDirectory() as d:
        db = CardDB.load(_write(Path(d), "cards.json", _fixture_cache()))
        r = CardResolver(db=db)
        assert r.describe("BG36_110") == "欢悦 1星 2/3 ABERRATION"
        assert r.describe("TB_BaconShop_HERO_91") == "时光扭曲者克罗米"


def test_resolver_load_missing_files_is_empty_not_crash():
    r = CardResolver.load(Path("/nope/cards.json"), Path("/nope/names.json"))
    assert len(r.db) == 0
    assert r.log_names == {}
    assert r.name_of("BG36_110") == "BG36_110"


# --- names.py -------------------------------------------------------------

class _FakeEntity:
    def __init__(self, card_id, name):
        self.card_id = card_id
        self.name = name


class _FakeEvent:
    def __init__(self, card_id, name):
        self.entity = _FakeEntity(card_id, name)


def test_collect_names_skips_unknown_entities():
    evs = [
        _FakeEvent("BG36_110", "欢悦"),
        _FakeEvent("BG31_149", "UNKNOWN ENTITY [cardType=INVALID]"),  # 噪声
        _FakeEvent("BG29_611", "N0NAME"),                            # 噪声
        _FakeEvent("", "没有 CardID"),
        _FakeEvent("BG28_504", ""),
        _FakeEvent("BG36_110", "重复不要覆盖"),
    ]
    names = collect_names_from_events(evs)
    assert names == {"BG36_110": "欢悦"}


def test_save_and_load_names_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "names.json"
        save_names({"BG36_110": "欢悦", "BG31_149": "剃刀沼泽地卜师"}, p, source="unit-test")
        names = load_names(p)
        assert names == {"BG36_110": "欢悦", "BG31_149": "剃刀沼泽地卜师"}
        payload = json.loads(p.read_text(encoding="utf-8"))
        assert payload["count"] == 2
        assert payload["source"] == "unit-test"


def test_load_names_missing_file_returns_empty():
    assert load_names(Path("/nope/names.json")) == {}
