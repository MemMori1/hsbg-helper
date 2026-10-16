# hsbg-helper

炉石传说 · 酒馆战棋的**实时辅助提示器**。

> **红线：只看不动。** 本工具读游戏日志、给建议，**绝不模拟鼠标键盘、绝不注入游戏**。
> 架构上不给执行层留任何接口（见 `docs/需求分析.md` §2.3）。

当前进度：**P0–P6 + P8–P14 已完成**。日志解析 → 盘面重建 → 卡牌数据 → 规则引擎 →
模型顾问 → Swift 悬浮框，整条链路已打通；开局英雄选择 / 饰品四选一 / 黑赐三选一 /
三连奖励等"待选"也已接入（规则展示 + 模型出概率）。模型层已迁到独立插件仓库
[`llm-odds`](../llm-odds)（本仓旧直连实现留作退路）。

---

## 快速开始

```bash
cd hsbg-helper

./hsbg sessions        # 列出所有日志会话（新→旧）
./hsbg check           # 用真实日志做 11 项自检
./hsbg state --until-line 92000   # 看第 92000 行时的完整盘面
./hsbg advise          # 规则建议 + 模型意见（llm-odds 插件）
./hsbg serve start     # 拉起 JevK5 本地推理服务（llama-server）
./hsbg panel --no-model   # 悬浮框的数据源（JSON Lines，一帧一行；跟随模式自动录像）
./hsbg replay          # 复盘回放：列出/回看自动落盘的对局
./run-tests            # 跑单元测试（375 个）
```

**零依赖**：纯 Python 标准库，跑在本机自带的 `/usr/bin/python3`（3.9.6），不需要 venv、
不需要 pip install。`./hsbg` 和 `./run-tests` 是包了 `PYTHONPATH=src` 的 bash 包装器。

**模型层可选**：不启动模型时自动降级为「只有规则建议」，不会报错。模型咨询走
llm-odds 插件（找不到插件才退回本仓旧直连）；两侧的服务都指向 `127.0.0.1:8090`。

---

## 命令行

| 命令 | 作用 | 常用参数 |
|---|---|---|
| `./hsbg sessions` | 列出会话目录、`Power.log` 大小、最后写入时间 | `--json` |
| `./hsbg parse` | 解析历史日志，打印事件 | `--limit N`、`--json`、`--kind` |
| `./hsbg check` | 对真实日志跑自检（行数守恒、归属自证、吞吐） | `--log <path>` |
| `./hsbg stream` | 实时跟随最新会话（增量 tail） | `--head N`、`--kind` |
| `./hsbg state` | **重放日志并渲染某一时刻的完整盘面** | `--until-line N`、`--cards-db` |
| `./hsbg watch` | 实时跟随并逐帧重绘盘面 | `--head N`、`--limit N` |
| `./hsbg cards` | 查看/校验卡牌库 | `--verify` |
| `./hsbg advise` | **规则引擎 + 模型意见（llm-odds 插件）** | `--until-line N`、`--no-model`、`--quiet`、`--show-evidence` |
| `./hsbg serve` | JevK5 服务状态 / 启动 / 停止 | `start`、`stop` |
| `./hsbg panel` | **悬浮框数据源**：一帧一行 JSON（跟随模式自动把每局录进 `replays/`） | `--until-line N`（单帧）、`--interval S`、`--no-model`、`--no-replay` |
| `./hsbg replay` | **复盘回放（P7）**：列出/回看自动落盘的对局 | `0`（0=最新）/`last`/文件路径、`--replay-dir` |

`--json` 输出 JSON Lines，方便直接管道给别的程序。

```bash
./hsbg advise --until-line 92000

建议  回合 15 招募 · 9 金 · 30 血 · 建议买「无面行动者」
商店
  考虑 无面行动者  4/2  4星  15.5分  与场面同族（3 只）；场面已满
升本  4本 → 5本  费用 10（金币不够）  不升  金币不足（要 10，只有 9）
模型意见（llm-odds）
  只买一张的话：复映无人机（商店第 2 项）  P=0.36，用时 0.76s
  现在升本？否  P(是)=0.40
  逐张买入意愿（按可能性排序）：
    蓝色扩音机（商店第 1 项）  不买  P(买)=0.46
```

---

## 代码结构

四层解耦（ADR-1），每层可以单独测试：

```
采集 logs.py / tailer.py  →  解析 parser.py / events.py
                                     ↓
                            状态重建 state.py   ← 8 人座位 / 商店 / 自家场面 / 金币 / 血量
                                     ↓
                            卡牌数据 cards.py + names.py   ← CardID → 中文名
                                     ↓
                  规则引擎 rules.py（P3 ✅）→ 模型顾问 provider.py + advisor.py（P4 ✅）
                                     ↓
                            渲染 render.py / 帧契约 panel.py
                                      ↓
                     展示：CLI 文本（P5 ✅）+ Swift 悬浮框（P6 ✅）
```

| 文件 | 职责 |
|---|---|
| `src/hsbg/config.py` | 路径常量、轮询间隔、`TailConfig` |
| `src/hsbg/logs.py` | 会话目录发现；判断游戏是否还在写日志 |
| `src/hsbg/tailer.py` | 增量 tail：半行缓冲、截断重置、会话轮换 |
| `src/hsbg/events.py` | `EventType` / `EntityRef` / `BlockRef` / `PowerEvent` / `ParseStats` |
| `src/hsbg/parser.py` | 单行 → 事件，纯流式 |
| `src/hsbg/state.py` | **盘面重建**：`GameState` / `Seat` / `ShopSlot` / `StateBuilder` |
| `src/hsbg/cards.py` | 卡表（`CardDB` / `CardResolver`）、缓存构建、HearthstoneJSON 下载 |
| `src/hsbg/names.py` | 从日志自举 `CardID → 中文名` |
| `src/hsbg/rules.py` | **规则引擎**：商店打分、升本、三连、血线预警、饰品/英雄实证推荐（纯规则，可离线单测） |
| `src/hsbg/trinkets.py` | **饰品实证数据**：HSReplay tier/选取率/平均名次 → 打分（`tools/fetch_hsreplay.py` 拉取） |
| `src/hsbg/heroes.py` | **英雄实证数据（S1）**：HSReplay 选取率/护甲 → 百分位打分（`tools/fetch_hsreplay.py --heroes` 拉取） |
| `src/hsbg/provider.py` | **JevK5 协议**：prompt 模板、字母 logprob 读法、概率校准（M4-lite：直连留作退路） |
| `src/hsbg/model_client.py` | **模型插件桥（M2）**：找 llm-odds → 子进程 JSON 契约（`ask-many`）；插件缺席才退回旧直连 |
| `src/hsbg/advisor.py` | **盘面 → 英文 evidence + 类型化提问**，`consult()` 总入口 |
| `src/hsbg/model_server.py` | 找 llama-server / GGUF，启停 JevK5 服务 |
| `src/hsbg/render.py` | 文本盘面渲染 |
| `src/hsbg/panel.py` | **悬浮框帧契约**：`PanelFrame`、`ModelAdvisorRunner`（模型调用与帧率解耦） |
| `src/hsbg/replay.py` | **复盘回放（P7）**：面板帧按局去重落盘 + 回看渲染 |
| `native/FloatingPanel/` | **Swift 悬浮框**（AppKit，`build.sh` 直编出 `.app`） |
| `src/hsbg/cli.py` | 十一个子命令 |
| `tools/verify_attr_rule.py` | 归属规则的语义自证（靠 `ENTITY_ID` 判对错，不靠数数） |
| `tools/winlist.swift` | 打印所有在屏窗口的矩形/owner（调窗口几何时用） |
| `tools/trace_decisions.py` | **逐回合打印决策框**，用来对照大神的运营节奏复盘规则 |
| `data/` | `cards_zhCN.json` / `cards_enUS.json`（各 1.2 MB 卡表）、`names_zhCN.json`（日志自举名表）、`trinkets_hsreplay.json`（饰品实证）、`heroes_hsreplay.json`（英雄实证） |
| `tests/` | 零依赖测试运行器 + 375 个测试 |

---

## 关键设计速览

- **模型接入（M2）**：优先 llm-odds 插件（`HSBG_ODDS_BIN` → PATH → 兄弟目录；
  `HSBG_ODDS_PROFILE` 档位、`HSBG_ODDS_REGISTRY` 注册表）；找不到插件退回旧直连
  （M4-lite：保留但不再演进）。插件契约与设计见 [`llm-odds`](../llm-odds) 的
  `docs/DESIGN.md`。
- **英文卡表**：`cards_zhCN`（展示）与 `cards_enUS`（只喂模型）双份，中英靠 CardID
  关联（模型 English-only，ADR-4）；缺英文表时静默退回中文。
- **待选**（英雄选择 / 饰品四选一 / 黑赐三选一 / 三连奖励）：规则层展示 + 实证
  推荐（饰品、英雄），模型做 `choice` 题出概率；**选项顺序不可重排**。
- **决策框（P11）**：悬浮框分两块 —— 决策框只回答"现在该做什么"，分析框回答"为什么"。
- **复盘回放（P7）**：面板跟随模式自动录像（一局一个 JSON 落 `replays/`），
  `hsbg replay` 回看。

---

## 测试

```bash
./run-tests           # 全部（375 个）
./run-tests -v        # 逐个打印
./run-tests -k 关键词  # 过滤**测试函数名**（不是文件名）
```

零依赖运行器（`tests/run_tests.py`，本机无 pytest）；模型层测试**全部离线**
（假 `urlopen` 桩 + 假插件脚本），不需要模型在跑。`./hsbg check [Power.log]` 是
另一个层次：跑真实日志断言行数守恒与归属语义，断言用**比例**而非绝对条数，
3 MB 的新会话日志和 96 MB 参考日志都能跑到 11/11。

---

## 文档

- `docs/需求分析.md` —— 完整需求、ADR、验收标准（含每项功能的实测与已知残留）
- `docs/实现笔记.md` —— **深挖/踩坑**：日志格式、模型层、英文卡表、待选与概率、
  卡牌机制覆盖（BG36）、决策框、悬浮框（Swift）、复盘回放
- `docs/赛季打法.md` —— 赛季运营笔记
- [`llm-odds`](../llm-odds) —— 模型插件（独立仓库：Python 库 / CLI / MCP）
- JevK5（本地决策模型）：<https://github.com/allebee/jevk5>
- HearthstoneJSON（卡表数据源）：<https://api.hearthstonejson.com/v1/latest/zhCN/cards.json>
