# hsbg-helper

炉石传说 · 酒馆战棋的**实时辅助提示器**。

> **红线：只看不动。** 本工具读游戏日志、给建议，**绝不模拟鼠标键盘、绝不注入游戏**。
> 架构上不给执行层留任何接口（见 `docs/需求分析.md` §2.3）。

当前进度：**P0–P6 + P8–P14 已完成**。日志解析 → 盘面重建 → 卡牌数据 → 规则引擎 →
JevK5 模型顾问 → Swift 悬浮框，整条链路已打通；饰品四选一 / 黑赐三选一 / 三连奖励
等"待选"也已接入（规则展示 + 模型出概率），**饰品四选一另有 HSReplay 实证推荐**
（tier + 选取率 + 平均名次，见 `src/hsbg/trinkets.py`）。

---

## 快速开始

```bash
cd hsbg-helper

./hsbg sessions        # 列出所有日志会话（新→旧）
./hsbg check           # 用真实日志做 11 项自检
./hsbg parse --limit 20   # 解析最新日志的前 20 行事件
./hsbg state --until-line 92000   # 看第 92000 行时的完整盘面
./hsbg advise          # 规则建议 + JevK5 模型意见
./hsbg serve start   # 拉起 JevK5 本地推理服务（llama-server）
./hsbg panel --no-model   # 悬浮框的数据源（JSON Lines，一帧一行）
./run-tests            # 跑单元测试（323 个）
```

**零依赖**：纯 Python 标准库，跑在本机自带的 `/usr/bin/python3`（3.9.6），不需要 venv、
不需要 pip install。`./hsbg` 和 `./run-tests` 是包了 `PYTHONPATH=src` 的 bash 包装器。

**模型层可选**：`./hsbg advise` 不启动模型时自动降级为「只有规则建议」，不会报错。
只有在你想要 JevK5 的第二意见时才需要 `./hsbg serve start`。

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
| `./hsbg advise` | **规则引擎 + JevK5 的建议** | `--until-line N`、`--no-model`、`--quiet`、`--show-evidence` |
| `./hsbg serve` | JevK5 服务状态 / 启动 / 停止 | `start`、`stop` |
| `./hsbg panel` | **悬浮框数据源**：一帧一行 JSON | `--until-line N`（单帧）、`--interval S`、`--no-model` |

`--json` 输出 JSON Lines，方便直接管道给别的程序。

```bash
./hsbg state --until-line 92000   # 看第 92000 行时的盘面

回合 15  招募  金币 9
商店（5 张）
  1. 蓝色扩音机 2星 1/3
  2. 复映无人机 4星 5/2
自家场面（7）
  1. ★奇瑰打击乐手 8/8   2. ★灰烬腐蚀者 12/12

./hsbg advise --until-line 92000 --quiet

建议  回合 15 招募 · 9 金 · 30 血 · 建议买「无面行动者」
商店
  考虑 无面行动者  4/2  4星  15.5分  与场面同族（3 只）；场面已满
升本  4本 → 5本  费用 10（金币不够）  不升  金币不足（要 10，只有 9）
模型意见（jevk5）
  只买一张的话：复映无人机（商店第 2 项）  P=0.36，用时 0.76s
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
| `src/hsbg/rules.py` | **规则引擎**：商店打分、升本、三连、血线预警、饰品实证推荐（纯规则，可离线单测） |
| `src/hsbg/trinkets.py` | **饰品实证数据**：读 HSReplay tier/选取率/平均名次 → 打分（`data/trinkets_hsreplay.json`，`tools/fetch_hsreplay.py` 拉取） |
| `src/hsbg/provider.py` | **JevK5 协议**：prompt 模板、字母 logprob 读法、概率校准 |
| `src/hsbg/advisor.py` | **盘面 → 英文 evidence + 类型化提问**，`consult()` 总入口 |
| `src/hsbg/model_server.py` | 找 llama-server / GGUF，启停 JevK5 服务 |
| `src/hsbg/render.py` | 文本盘面渲染 |
| `src/hsbg/panel.py` | **悬浮框帧契约**：`PanelFrame`、`ModelAdvisorRunner`（模型调用与帧率解耦） |
| `native/FloatingPanel/` | **Swift 悬浮框**（AppKit，`build.sh` 直编出 `.app`） |
| `src/hsbg/cli.py` | 十个子命令 |
| `tools/verify_attr_rule.py` | 归属规则的语义自证（靠 `ENTITY_ID` 判对错，不靠数数） |
| `tools/winlist.swift` | 打印所有在屏窗口的矩形/owner（调窗口几何时用） |
| `tools/trace_decisions.py` | **逐回合打印决策框**，用来对照大神的运营节奏复盘规则 |
| `data/` | `cards_zhCN.json` / `cards_enUS.json`（各 1.2 MB 卡表）、`names_zhCN.json`（日志自举名表）、`trinkets_hsreplay.json`（饰品实证） |
| `tests/` | 零依赖测试运行器 + 323 个测试 |

---

## 日志格式：踩过的坑

这些是从真实 92 MB 日志里**实测**出来的，不是猜的。改解析器前务必先读这一节。

### 1. 日志在哪、会不会轮转

```
/Applications/Hearthstone/Logs/
  └── Hearthstone_2026_10_10_21_11_09/     # 每次启动游戏新建一个
        └── Power.log                       # 单文件 92～105 MB
```

⚠️ 本机 `log.config` **不存在**，`client.config` 里 `FileSizeLimit.Int=-1`
→ **日志不轮转、无限增长**。所以 tailer 只做"发现 + 增量读"，不假设大小上限。

### 2. 一份日志里有 4 局，只有约 2 局是战棋

`CREATE_GAME` 出现 4 次，但 `GameType=GT_BATTLEGROUNDS` 只出现 2 次。
不能见到 `CREATE_GAME` 就当战棋局。

### 3. ⚠️ 命令词可以带缩进

```log
D 21:12:25.2665920 GameState.DebugPrintPower() - TAG_CHANGE Entity=17 tag=RESOURCES value=3
D 21:12:25.2665920 GameState.DebugPrintPower() -     TAG_CHANGE Entity=17 tag=TURN value=2
```

用 `re.match(r"^TAG_CHANGE")` 锚行首会**少拿一个数量级**（实测 6,999 vs 146,027）。
必须用 `search()`。

### 4. ⚠️ 块内裸 `tag=` 行没有实体前缀

```log
D 21:12:25.2665920 GameState.DebugPrintPower() - BLOCK_START BlockType=TRIGGER Entity=[... id=75 ...]
D 21:12:25.2665920 GameState.DebugPrintPower() -             tag=CONTROLLER value=14
```

实测 **115,019 行**这种形态。

**归属规则**：缩进 `k` 的裸 tag 行，属于 **`k − 4` 深度上最近声明的实体**
（`PowerLogParser._entity_by_indent`）。

⚠️ **`BLOCK_START` 的 `Entity=` 是"谁触发了这个块"，不是"属性属于谁"** ——
块里的 `FULL_ENTITY` 属于它自己。`BLOCK_START` **不能**登记进深度表，否则
766 条 `ATTACHED_TAG` 归属全错。

### 5. `CREATE_GAME` 里的实体声明

```log
D ... - CREATE_GAME
D ... -     GameEntity EntityID=16
D ... -         tag=CARDTYPE value=GAME
D ... -     Player EntityID=17 PlayerID=6 GameAccountId=[hi=... lo=...]
```

声明缩进 4、属性缩进 8（同样满足 +4）。每局 6 行（GameEntity 2 + Player 4），
是 `PLAYER_ID` / `HERO_ENTITY` 的唯一来源，连带的属性行共 352 行。

⚠️ **`tag=HERO_ENTITY` 不是身份自证** —— 它写的是"这个玩家的英雄是谁"
（`Player EntityID=17` 的属性里有 `tag=HERO_ENTITY value=35`）。只有 `tag=ENTITY_ID`
能自证宿主身份。

### 6. 两份 source 会重复打同一动作

`GameState.DebugPrintPower()` 与 `PowerTaskList.DebugPrintPower()` 把同一动作各打一遍
（344,567 vs 334,597 行）。**只取一份**（ADR-7），否则事件翻倍。

### 7. 卡牌中文名是可以拿到的

`grep -c "tag=NAME"` = **0**，但方括号实体形态和 `META_DATA` 的 `Info[i]` 明细
**直接带本地化名，且与 CardID 相邻**：

```log
Info[0] = [entityName=欢悦 id=312 zone=PLAY zonePos=3 cardId=BG36_110 player=14]
```

一份 92 MB 日志能自举出 **496 条 `CardID → 中文名`**。

---

## 模型层（JevK5）：踩过的坑

这一节是从"以为能跑"到"真的跑通"之间踩出来的，**换模型或换服务前务必先读**。

### 8. LM Studio 的服务不能用来跑 JevK5

JevK5 的置信度全靠**答案位置那个字母的 softmax**，所以必须能读到 logprob。
而 LM Studio（本机 6324 端口）的三个端点全都不给：

| 请求 | 结果 |
|---|---|
| `/v1/completions` + `logprobs`/`n_probs` | `HTTP Error 400: Bad Request` |
| `/v1/completions` 不带 | `"logprobs": null` |
| `/v1/chat/completions` + `chat_template_kwargs.enable_thinking=false` | 参数不生效，100% token 记进 `reasoning_tokens` |
| `/api/v0/chat/completions` 非标准 body | 返回没有 `choices` 的响应 |

**出路**：LM Studio 自带的 llama.cpp 引擎里就有 `llama-server`：

```bash
~/.cache/lm-studio/extensions/backends/llama.cpp-mac-arm64-apple-metal-advsimd-2.56.0/llama-server \
  -m ~/.cache/lm-studio/models/alibiserikbay/JevK5-GGUF/jevk5-4b-v0.2-Q4_K_M.gguf \
  -c 8192 -ngl 99 --port 8090 --host 127.0.0.1
```

它的 `/completion` + `n_probs` 正常返回 `completion_probabilities[0].top_logprobs`。
`./hsbg serve start` 就是干这个的（会先 `lms unload` 腾内存，4B Q4_K_M 约占 2.7 GB）。
注意 `start` / `stop` / `status` 是**位置参数**，不是 `--start`。

### 9. ⚠️ 必须先 `/tokenize`，再把 token id 喂 `/completion`

直接把 prompt 字符串当 `prompt` 传，`<|im_start|>` 会被当成 6 个普通字符，
整个 chat 模板结构就废了。官方 `gguf.py` 也是这么做的：
`/tokenize`（`add_special:false, parse_special:true`）→ `/completion`
（`n_predict:1, n_probs:40, temperature:0, cache_prompt:false`）。

### 10. ⚠️ `/health` 只认 GET

POST 会 404。而且要小心 `HTTPError` 是 `URLError` 的**子类**——`except` 顺序写反
会把 404 报成"连不上服务器"，排查时会被带偏。

### 11. 概率要除以校准温度，不是直接 softmax

官方给的各档校准温度：**2B = 1.42**、v0.2 4B = 1.532、v0.3 4B = 1.367、9B = 1.089。
概率 = `softmax(logprob / temperature)`。字母不在 top-k 里时用
`min(seen) - 2.0` 兜底（官方 `MISSING_MARGIN`）。

⚠️ **温度要认版本号**：4B 的 v0.2 与 v0.3 差得不少（1.532 vs 1.367）。用错会让概率
整体偏平 —— 而"偏平"恰好是最容易被误判成"模型不行"的症状。
`model_server.size_key()` 从 GGUF 文件名解析出 `4b-v0.2`，
`advisor_from_args()` 在没给 `--model-size` 时**从实际会被加载的那个 GGUF 反推**。

### 11b. 挑模型档位：不是"越大越好"

`./hsbg serve start` 会自动挑 GGUF，顺序是 **4B > 2B > 9B**，同档位内再比量化
（Q8_0 > Q6_K > Q5_K_M > Q4_K_M）。**刻意不按文件大小排**，因为：

| 档位 | JevBench v1.4 hard tier | 校准 ECE | 结论 |
|---|---|---|---|
| **4B** | **0.784** | **0.054** | 最好，首选 |
| 9B | 0.730 | 0.126 | 反而更差，且更吃内存 |
| 2B | — | — | 最快但区分度不足（实测 `P(买)` 全挤在 0.42~0.48） |

实测同一盘面（`Power.log --until-line 92000`）的逐张买入意愿：

```
2B v0.2 Q8_0   0.44 / 0.42 / 0.41 / 0.41 / 0.40     ← 极差 0.04，等于没表态
4B v0.2 Q4_K_M 0.44 / 0.40 / 0.21 / 0.20 / 0.13     ← 极差 0.31，明确表态
```

代价是慢约一倍（同一盘面 11.6 s → 22.8 s）。所以 `--model-wait` 默认给了 40 s
（只影响 `--until-line` 单帧模式；跟随模式的帧一直往外发，答案到了自动补）。

⚠️ **模型会自相矛盾**：同一次运行里"只买一张的话"答 `复映无人机`，
而"买复映无人机吗"只给 P=0.13（全场最低）。JevK5 每个问题**独立打分**，
不做跨问题一致性校验，所以别只看单个答案 —— 结合规则层一起看。

### 12. 题面必须是官方 JSON 形状，而且必须是英文

user 消息是一个 JSON：

```json
{"evidence": "...", "criterion": "...", "options": [{"letter": "A", "description": "..."}]}
```

我最早自创题面（把问题写成自然语言）得到 4 题错 2 题；换成官方形状后立刻合理
（"凑三连该不该买" → C，与规则引擎判断一致）。**JevK5 是 English-only**，
所以 `advisor.build_evidence()` 里做了盘面 → 英文的翻译层。

### 13. 酒馆等级在 `PLAYER_TECH_LEVEL`，不是 `TECH_LEVEL`

`TECH_LEVEL` 在战棋里是**随从星级**和升本按钮的目标档位；玩家自己的等级写在
英雄实体的 `PLAYER_TECH_LEVEL` 上。`Entity.tech_level` 先读前者、空则退回后者。

### 14. 升本费用只能读按钮上的实时 `COST`

同一档位的费用会浮动（提前升本更便宜、上回合战败打折）：

| 按钮 | 实测 `COST` |
|---|---|
| `TB_BaconShopTechUp03_Button` | 4 / 5 / 6 |
| `TB_BaconShopTechUp05_Button` | 9 / 10 / 11 |
| `TB_BaconShopTechUp06_Button` | 9 / 10 / 11 |

硬编码费用表只能当兜底，而且**必须限定 `ZONE=PLAY`**（`SETASIDE` 里是陈旧值）。

---

## 英文卡表：为什么有两份 cards 数据

`data/cards_zhCN.json`（展示）与 `data/cards_enUS.json`（只喂模型）是**同一份数据的两个语言**。

**为什么不能只用中文卡表**：JevK5 是 English-only（ADR-4），把中文卡名/卡文塞进
evidence 会让 2B 模型基本读不懂。早期实测：evidence 里「商店」段全是中文名，
模型的回答质量明显差。

**为什么不能拿中文名去英文卡表反查**：中文名与英文名不是直译，日志里还有第三套
皮肤短名。实测：
- `BG20_HERO_100_SKIN_A4` 日志 `洛卡拉` / 中文卡表 `奥术战士洛卡拉`
- `TB_BaconShop_HERO_41_SKIN_F` 日志 `雷诺·杰克逊` / 中文卡表 `废铁飞贼雷诺`

唯一的正解是**靠 CardID 关联**（`CardDB.get()`），CardID 是语言无关的。

```bash
./build-cards --lang enUS          # 重建英文卡表（联网）
./build-cards --lang enUS --input FILE   # 离线重建
```

代码里的落点：
- `CardResolver.name_en_of(card_id)`：英文名，查不到退回中文名
- `RulesEngine(db_en=...)`：待选项的 `name_en`/`text_en`
- `build_evidence(..., db_en=...)`：商店/场面/手牌/饰品/待选全走英文

⚠️ 缺 `data/cards_enUS.json` 时全链路**静默退回中文**，功能不中断（但模型质量下降）。

---

## 待选（饰品四选一 / 黑赐三选一）与模型概率

待选分两类处理：

**饰品四选一 —— 规则层给实证推荐 + 模型出概率**。HSReplay 提供了饰品的
tier / 选取率 / 终局平均名次（`data/trinkets_hsreplay.json`，由 `tools/fetch_hsreplay.py`
拉取），规则层据此打分并用 ★ 标出推荐（`src/hsbg/trinkets.py` + `rules.py`）：

```
小饰品四选一  （还没选）  小型饰品商店将于5回合后开启！
  ★ 1. 朝圣小鬼标签  A级 · 选取37% · 平均名次3.82  每回合中，有一张恶魔牌可用生命值购买…
    2. 爱心吊坠     D级 · 选取14% · 平均名次4.54  在你对一个友方随从施放法术后…
```

**黑赐三选一 / 三连奖励 / 卡牌效果发现 —— 规则层只展示，模型出概率**。这些没有
可复现的样本（黑赐是"随从 + 赐福"的任意组合，420 张饰品的兄弟问题在赐福上复现），
凭卡文猜分数只会误导，所以"该选哪个"完全靠 JevK5 做 `choice` 题：

```
模型意见（jevk5）
  伦德尔标签  P=0.35，用时 1.08s
    概率分布：伦德尔标签 35% · 朝圣小鬼标签 28% · 战鼓 21% · 爱心吊坠 16%
```

实现要点（`src/hsbg/trinkets.py` + `src/hsbg/advisor.py`）：

**实证分（只对饰品）**
- 公式：`score = tier 基础分 + (4.2 − 平均名次) × 3`，
  `S/A/B/C/D/F = 3.0 / 1.5 / 0.5 / −0.5 / −1.5 / −3.0`
  - 参照点 4.2 = ALL 分段全体饰品平均名次的**中位数**（实测）；名次项实际贡献
    约 ±2.6 分，与 tier 的 ±3 分同量级，两者不互相淹没。
  - 固定用 `brackets.ALL`（样本最大）。`TOP_1_PERCENT` 的 tier 口径不同
    （同一件饰品判 F 级的数量是 ALL 的 5 倍），**两套不能混用**。
- ⚠️ **多彩罗盘**（`BG30_MagicItem_426` 小/大型）在 HSReplay 里是**按大厅种族组合
  分行**的（同一 `card_id` 10 行）。取**平均名次的中位数行**作为代表值并标
  "大厅种族相关"——取最好 = 假设大厅种族正好是它的最佳组合（高估），大厅种族
  在日志里读不到，中位数是诚实的代表值。
- ⚠️ **查不到 = `None`，绝不能当 0 分**：新补丁刚上的饰品 HSReplay 还没统计到，
  0 分会让它看起来比 F 级还差。缺数据的选项不画分、不参与推荐。
- 部分选项缺数据时仍然推荐"有数据的里面最好的"，但尾巴上带一句
  `1 项无实证数据`（新饰品往往很强，闷头推荐一个 D 级老饰品会误导）。
- 实证统计会**同时进 evidence 与 `choice` 题面**（英文一行
  `HSReplay tier S, picked 37%, avg place 3.82`，ADR-4）——模型是在实证之上
  再叠加盘面/阵容判断，不是从零猜。
- ⚠️ **选项顺序不许重排**：面板上的 `1..N` 就是游戏里的 offer 顺序，模型答案按
  序号翻译回中文名（`_pending_choice_text`）。规则层的推荐只加 ★ 标记。
- `data/trinkets_hsreplay.json` 缺失/损坏时**静默降级**为老行为（只展示不推荐），
  与缺 `cards_enUS.json` 的约定一致。

**题面与答案翻译（`advisor.py`）**
- `choice_question()` 把待选做成 `choice` 题，选项文本写成 `"N. 英文名 — 英文卡文"`
  - ⚠️ **不能**改成 `(id, 文本)` 元组：`Question.options()` 对 `choice` 题的既有契约是
    "裸文本即选项 id"，改成元组会同时破坏 `pick_shop` 的答案翻译。
  - 序号靠 **`"N. "` 数字前缀**带出去，翻译层再换回中文卡名。
- `choice_ranking()` 把概率翻成 `[(中文名, p)]` 并降序排序（面板/CLI 用）
- `_pending_choice_text()` 把模型回的选项序号换回中文名
  - ⚠️ 选项原文是 `"2. Amplifier — ..."`，直接找纯数字 token 找不到
    （`"2."` 带点，`str.isdigit()` 为假）→ 必须先剥前导序号。
- 待选题在 `build_questions()` 里**排在最前**：它是当前唯一会卡住回合的决策。

`hsbg panel` 的单帧模式下模型默认最多等 **20s**（`--model-wait`，默认 80 轮 × 0.25s）。
⚠️ 原先的 5s 上限会稳定超时——实测"待选 + 只买一张 + 升本"三问就要 5.75s，
输出一帧「模型思考中…」却没有任何意见。

---

## 卡牌机制覆盖（BG36 当前版本）

### 「发动」不在 `mechanics` 字段里

这是最容易被静默漏掉的一条。实测 `BG36_099`（蚀脑怪）：

```python
{'type': 'MINION', 'techLevel': 2, 'attack': 3,
 'text': '<b>发动（0）：</b>弃掉一张牌以使你的<b>神明</b>获得+2/+2。',
 'mechanics': None}          # ← 是 None，不是 ['发动']
```

`rules.py` 里的 `parse_text_keywords()` 从卡文模板里抠关键词
（`发动（N）` / `复仇（N）` / 塑造法术 / 进击），权重按费用分档
（`发动(0)` 1.6 > `发动(1)` 1.2 > `发动(2)` 0.9 —— 费用是**代价**，越低越强）。

⚠️ **关键词必须是白名单**。卡文里 `随从消耗(1)`、`出售获得(0)`、`铸币减少(2)`
长得跟关键词一模一样，用宽松正则
（`[\u4e00-\u9fff]{2,4}[（(]\d+[）)]`）会一口气捞进 **100 张风马牛不相及的卡**
（异变、英雄技能、饰品…）。加上白名单后全库仍有 353 张卡命中。

⚠️ **还要去重**：`BG21_001`（苍鳞鳄鱼）既有 `mechanics: ['AVENGE', ...]`，
正文又写 `复仇（2）`。不去重就是 1.0 + 1.2 = 2.2 分。`_TEXT_TO_MECHANIC` 负责归一。

### 「黑暗之赐」（黑赐）有日志可判

| 线索 | tag / CardID | 含义 |
|---|---|---|
| 按钮在场上 | `BG36_Button_DarkGift` | 「黑暗发现」按钮 |
| **现在能点** | `BACON_DARK_GIFT_PRESSABLE_VFX=1` | ← 判据是**这个** |
| 机制是否启用 | `BACON_DARK_GIFTS_ACTIVE` | ⚠️ 整局只在开局出现 2 次，**不是**"此刻可点" |
| 随从已挂赐福 | `HAS_DARK_GIFT=1` | 随从侧 |
| 挂的是哪个 | `DARK_GIFT_ENTITY=<实体 id>` | 指向赐福本体卡 `BG36_MidGameEffect_000tNN` |

赐福本体是**卡**，卡表里有中文名：`t11`=双重幻象 / `t13`=鹰身人之爪 /
`t18`=不断复制 / `t78`=高超效率。`rules.py` 的 `_dark_gift()` 把这些翻出来显示。

⚠️ `BG36_MidGameEffect_010`（`酒馆战棋黑暗之赐[DNT]`）是**机制总开关**，
不是某个赐福，必须从"已知赐福"里排掉。

⚠️ 能点的时候一定要提醒——这是**免费收益**，漏点就是白亏：
`⚠ 「黑暗发现」可以点了——免费三选一，别忘拿`

#### ⚠️ 黑赐三选一的选项是**随从**，但强弱取决于**赐福**

这条不做，模型就是在瞎猜。实测（`Hearthstone_2026_10_14_21_48_48/Power.log`
33854 与 50577 两个待选块）：

| 选项随从 | 它自带的赐福 | 赐福给的东西 |
|---|---|---|
| 虔诚的萨特唤魔者 `BG33_155` | 鹰身人之爪 `…000t13` | 圣盾、风怒 |
| 受困的钟舌恶魔 `BG36_730` | 不断复制 `…000t18` | 每 2 回合复制一张 |
| 恶臭侵蚀怪 `BG36_112` | 双重幻象 `…000t11` | 拿一张额外复制 |
| 海藻护卫 `BG36_701` | 高超效率 `…000t78` | 发动触发两次 |
| 克熙尔割喉者 `BG36_106` | 防御献祭 `…000t2` | +10 血、亡语传血 |

**同一个随从可以配完全不同的赐福**（第二局里 `受困的钟舌恶魔` 配的是
`死亡之口`，第一局配的是 `不断复制`）——所以"这只随从身材好不好"只是
一半信息，另一半全在赐福上。

关联方式与两个必须注意的坑：

1. 赐福关系写在**选项随从实体**上的 `DARK_GIFT_ENTITY=<赐福实体 id>`，
   赐福实体由 `FULL_ENTITY - Creating ID=N CardID=BG36_MidGameEffect_000tNN`
   先建出来。
2. ⚠️ **这两行都比 `DebugPrintEntityChoices` 头行早**（实测早 14 ms），
   所以选项挂上盘面时一定读得到。
3. ⚠️ **但 `DARK_GIFT_ENTITY` 每局末态会被清成 `value=0`**，晚查就没了 ——
   必须在 `StateBuilder._attach_gifts()`（选项发布时）抄一份快照，
   不能在规则层回头查。`value=0` / 空串都要当"没有赐福"，不能当实体 id。

落点：`ChoiceItem.gift_card_id / gift_name / gift_note`（中文展示）、
`gift_name_en / gift_text_en`（喂模型）。赐福在 evidence 里写成
`Dark Gift: Harpy's Talons (Divine Shield, Windfury)`，并且排在
`note_en` 最前 —— 它是这条选项的主要决策依据，不能排在卡文后面。
展示层（CLI 与 Swift 面板）把赐福**单独画一行**（金色），
不塞进 `note` 里被卡文挤掉。

### 实体名不可信

实测日志里有 **2 万多次** `UNKNOWN ENTITY [cardType=INVALID]` 和 301 次 `N0NAME`。
直接拿来显示会变成"黑暗之赐：UNKNOWN ENTITY [cardType=…]"，
`RulesEngine._is_known_name()` 负责过滤（⚠️ 用 `startswith` 判前缀，不要用 `==`，
实测形态带后缀）。

⚠️ 光过滤还不够：**卡表里其实有名字**。实测 `BG31_881` 的日志名是
`UNKNOWN ENTITY [cardType=INVALID]`，卡表里是「时间管理」。所以
`_best_name()` 的择优顺序是「日志名 **且** 不是占位名 **且** 不像 CardID → 日志名，
否则卡表名」，不是简单的"日志名优先"。

### 酒馆法术（Tavern Spells）走的是另一条购买路径

**最容易整个漏掉的一节** —— 法术**不在 `st.shop` 里**，也不是 `zone=PLAY` 的普通随从：

| 项 | 随从 | 酒馆法术 |
|---|---|---|
| 槽位宿主 CardID | `TB_BaconShop_DragBuy` | **`TB_BaconShop_DragBuy_Spell`** |
| 指向实体的标签 | `tag=2442` | **同样 `tag=2442`** |
| 买价 | 恒 3 金 | **`COST` 0~4 金**（也有免费法术） |
| 卡表 type | `MINION` | `BATTLEGROUND_SPELL` |
| 占不占场面格子 | 占 | **不占** |

只认 `TB_BaconShop_DragBuy` 会让整个建议里看不到那张 1 金的法术
（实测一局里有「主厨甄选 2 金 / 酒馆币 1 金 / 联盟旗帜 1 金 / 时间管理 4 金」）。
`StateBuilder._refresh_shop()` 现在两种宿主都收，放进 `GameState.spells`
（`shop_spells` 是同名的兼容别名），`RulesEngine._score_spells()` 单独打分
（法术与随从**不是同一把尺子**，不能直接比大小）。

法术**按效果分类**打分（判据全是卡文正则，见 `src/hsbg/rules.py` 的
`SPELL_EFFECT_PATTERNS`）：`给牌 +2.5` > `涨钱上限 +2.0` > `刷新/给身材 +1.0`
> `给钱/给关键词 +0.5` > `变身 −1.0`。⚠️「酒馆币」是 1 金买 1 金**净 0**，
所以 `给钱` 权重必须低；「钻探原油」（铸币上限 +1，永久）拆成 `涨钱上限`
单独给高分。全局增益（"使你的随从"）另按场面数 `+0.4×(n−1)` 放大，
单体增益在空场 `−1.5`。

⚠️ 分类前**必须去掉空白**：卡表正文剥掉 `<b>` 后会留空格
（实测「随机使酒馆中的一个随从变为 金色」），否则 `变为金色` 匹配不上。

⚠️ 两条由此而来的坑：

1. **法术与随从的分数不能直接比**：法术起手 6 分、随从按身材算（早期随从普遍
   5~9 分）。直接比大小会让决策框在**回合 1 空场时去买法术**（实测原话：
   `回合 1 · 3 金 · 空场 → 买 招募新人 2 金`）。修法是**按类别排序**：场上不到
   3 只时优先买身材，撑起来之后才允许买法术。
2. **槽位可能指向还没解析出来的实体**：`card_id` 有值但名称是占位符，
   不能进建议列表（见上一节）。

### 饰品四选一 / 黑赐三选一：选项不在 Power 流里

**最容易漏的一节**。四选一的选项**完全不在 `GameState.DebugPrintPower()` 里**，
而在另外两条 source：

| source | 内容 |
|---|---|
| `GameState.DebugPrintEntityChoices()` | 头行（含 `ChoiceType` / `Player`）+ `Source=` + `Entities[i]=` |
| `GameState.SendChoices()` | 玩家**实际选了哪个** → `m_chosenEntities[0]=` |

两条都只在 `GameState` 分支有（`PowerTaskList` 没有对应输出），所以它们进了
`DEFAULT_SOURCES` 不会像 `DebugPrintPower` 那样把事件翻倍。

```
D 21:49:26 GameState.DebugPrintEntityChoices() - id=3 Player=测试玩家#1234 TaskList=1258 ChoiceType=GENERAL CountMin=1 CountMax=1
  Source=[entityName=小型饰品 id=300 zone=PLAY zonePos=0 cardId=BG30_Trinket_1st player=2]
  Entities[0]=[entityName=朝圣小鬼标签 id=3062 zone=SETASIDE cardId=BG32_MagicItem_821 player=2]
```

**同一套机制覆盖四种场景**（靠 `Source=` 区分）：起手换牌（`ChoiceType=MULLIGAN`，
`Source=GameEntity`，选项 `zone=HAND`）、黑暗之赐（`Source` 是
`cardId=BG36_MidGameEffect_010`，选项是**随从**）、**小/大饰品**（`Source` 是
`cardId=BG30_Trinket_1st` / `_2nd`）、卡牌效果触发的发现（三连奖励、
`死亡护命匣`、`无限潜力`…`Source` 就是那张卡）。

⚠️ **`CountMin` / `CountMax` 是"选几个"不是"给了几个"**。实测每块都是
`CountMin=1 CountMax=1`，而选项有 3–4 个（统计：`{1: 4, 2: 3, 3: 4, 4: 3}`）。
拿它判"选项收全"必然错。

⚠️ **`Source=` 行与 `Entities[i]=` 行都不带 `id=`**，认领只能靠"当前正在收选项的块"。
于是状态层要有个缓冲（`StateBuilder._choosing`），而且**只有头行（带 `id=`）能开新块**：
把 `Source=` 行也当成"收尾上一个块"会让 `_choosing` 立刻变 None，后面所有选项全被丢
（这是实测踩过的真 bug）。

⚠️ **选项是逐行来的，必须原地增补**：最后一个选项行之后**没有空行之类的结束标记**，
"收全"那一刻收不到任何事件。所以 `_publish_choice()` 一有选项就挂同一个对象到
`state.pending_choice` 上，规则层读 `state` 时自然看到最新完整列表。

⚠️ **饰品名取卡表名，不要取日志名**：日志里饰品是 `朝圣小鬼标签`、
`伦德尔标签` 这种内部占位名，卡表名才是 `战鼓`、`爱心吊坠`。

**大饰品（`BG30_Trinket_2nd`）实测形态**（96 MB 参考日志，回合 17）：
```
大饰品四选一（还没选）  大型饰品
  1. 达拉然奶酪轮  酒馆中的随从拥有+2/+2。你每刷新4次后提升。（还剩4次！）
  2. 记忆之球      在你的回合结束时，获取你施放的上一个酒馆法术的3张复制。
  3. 沙斯亚尔神坛  在你弃掉一张法术牌后，随机获取一张畸变怪。
```

---

## 决策框（P11）：两块分工，各自只干一件事

悬浮框是**两个窗口**：

- **决策框**（上方，矮）——只回答"**现在该做什么**"：一个大字动作 + 右侧费用 + 一行理由。
- **分析框**（下方，高）——回答"**为什么**"：商店打分、模型概率、场面、血线、预警。

分工的直接后果：**分析框的标题不再复述动作**。早先只有一个窗口时，标题必须
说"建议买 X"，否则会和下面的升本栏打架；分成两块后还复述就变成同一句话出现两遍。

### 决策框的优先级

| 顺序 | 动作 | 触发 | 说明 |
|---|---|---|---|
| 0 | 选择 | `pending_choice` | **已经弹出的模态选一**（饰品四选一 / 黑赐三选一）。卡着回合，不点没法继续 |
| 1 | 升本 | `curve` | 基准曲线说该升了（见下表） |
| 2 | 攒钱 | `save_for_level` | 曲线说要升、但金币不够，且差的不多 |
| 3 | 买 | `triple` | 能凑三连 |
| 4 | 买 | `shop` | **店内第 1 名**且过了地板分（见下节） |
| 5 | 买 | `thin_board` | 场面 <5、血线 ≤15，挑个「考虑」的撑着 |
| 6 | 刷新 | `free_refresh` | 有免费刷新次数 |
| 7 | 选择 | `dark_gift` | 没别的事干时，才升级成主动作 |
| 8 | 观望 | `idle` | |

### 买牌动作按「店内排名」派发，不按绝对分数线

旧规则要求 `score >= 18` 才说「买」，结果是决策框**几乎从不说买**。
用 `tools/calibrate_shop.py` 拿 5 份真机日志 / 90 个商店快照校准后，结论是
**分数对"买不买"没有预测力，但对"买哪张"有效**：

| 判据 | 说「买」的店 | 精确率 | 召回率 |
|---|---|---|---|
| 旧：店内最高分 ≥ 18 | 24/90 | 92% | 29% |
| 旧：店内最高分 ≥ 11 | 55/90 | 87% | 64% |
| **新：按店内排名** | 63/90 | **92%** | **77%** |

（真人买牌店占比的基础率 = 33%。）

- 用「店内最高分」预测"这回合会不会买"，精确率在 0~24 分的**每个阈值上都贴着基础率
  84% 不动** —— 调阈值是死路。真正决定买不买的是金币、场面格子和要不要升本。
- 但排序有效：真人买走的那张，店内排名第 1 占 **47.1%**、前 2 占 **66.7%**（随机约 30%）。
- 而且店内第 1 名的买入率按分数段相当平：4-7 分 **91.7%**、7-10 分 100%、
  10-13 分 94.4%、13-16 分 69.2%。

所以现在的规则（`RulesEngine._assign_shop_actions`）：凑三连 → `买`；第 1 名且
`score >= BUY_TOP_MIN`（**4.0**）→ `买`；第 2、3 名 → `考虑`；之后 → `跳过`；
场面已满时把第 1 名降级成 `考虑` 并注明「场面已满」。

⚠️ 地板分**只能压到 4 分**：实测最低的 4-7 分段买入率仍有 91.7%，而一张 1 星 2/3 的
裸随从就只值 4.5 分 —— 地板设 6.0 会把大量真人真会买的牌判成「考虑」。

⚠️ **有牌可买时买牌优先于免费刷新**（免费刷新是买完之后顺手用的），所以
`test_decision_asks_for_the_refresh_when_it_is_free` 的夹具用的是**空商店**。

### 基准曲线（来自水神 Waterloo 那套公式化打法）

| 回合 | 目标本数 |
|---|---|
| 1–2 | 不要求 |
| 3–6 | 2 本（**四费升二本约占 75% 对局**，是最主流的一条） |
| 7–9 | 3 本 |
| 10–12 | 4 本 |
| 13–15 | 5 本 |
| 16+ | 6 本 |

偏离曲线时决策框会标 `off_curve`，并把"为什么偏离"写进理由行。

### 两条踩过的坑

**① 升本守卫不能拦低本阶段。** 第四定理说"切勿在不该升本时大代价贸然升本"，
但四费升二本时场上常常**一只随从都没有**。所以守卫的条件是「3 本以上 + 升完
只剩不到 2 金 + 场面 <5」，低于 3 本一律不拦。

**② 「黑暗发现可点」不能当主动作。** 它的判据是按钮上的
`BACON_DARK_GIFT_PRESSABLE_VFX`，而实测这个 flag **连续亮 4 个回合**
（T5 `MAIN_START_TRIGGERS` 亮 → T9 `MAIN_ACTION` 灭）。放在最前面的话，决策框
会连续 4 回合只说"先选牌"，把买牌/升本的建议全挡住。现在它只在"本来没事干"
（本该「观望」）时才升级成主动作，否则退成理由行末尾的一句"（黑暗发现可点）"。

### 复盘工具

`./tools/trace_decisions.py <Power.log>` 逐回合打印决策框，可以直接和真人的
运营节奏对照（实测在 96 MB 日志上 2.5 秒跑完）：

```
回合    满金    本    场面   决策框            触发              店最高       理由
3     4     1    1     升本 2本  4 金     curve           11.1 欢悦   曲线：3 回合该到 2 本
7     6     2    2     升本 3本  5 金     curve           7.1 心灵泥魔  曲线：7 回合该到 3 本（黑暗发现可点）
13    9     3    6     升本 4本  5 金     curve           10.3 木乃伊工匠 曲线：13 回合目标 5 本，先上 4 本（黑暗发现可点）
```

⚠️ 取样必须在**事件流**里判回合/阶段（`TAG_CHANGE GameEntity tag=TURN` 与
`tag=STEP`），**不能**每个事件都读 `builder.state` —— 读一次就触发一次全量惰性
刷新，整局会从 2 秒退化成 2 分钟（实测踩过）。

`./tools/calibrate_shop.py` 是**买牌阈值校准**工具：拍商店快照 → 打分 → 和真人
实际买了什么对齐，输出「引擎说买的店 / 精确率 / 召回率」。改打分权重或动作规则后
跑一遍，能立刻看出是变好还是变坏（上面的 92% / 77% 就是它给的）。
⚠️ 同样必须**先筛事件再读 state**。

**调试开关**：`HSBG_PANEL_FORCE_SHOW=1` 让面板无视"炉石是否在前台"始终可见。
⚠️ 必须**直接跑二进制**，别用 `open` —— `open` 走 LaunchServices，不传环境变量：

```bash
pkill -f hsbgpanel; rm -f /tmp/hsbgpanel.log; touch /tmp/hsbgpanel.log; chmod 666 /tmp/hsbgpanel.log
HSBG_PANEL_FORCE_SHOW=1 ./native/FloatingPanel/build/hsbgpanel.app/Contents/MacOS/hsbgpanel &
```

---

## 悬浮框（Swift）：踩过的坑

`native/FloatingPanel/` 是一个 AppKit 悬浮面板，吸附在炉石窗口右侧，数据来自
`./hsbg panel` 的 stdout（换行分隔 JSON，不走 HTTP）。

```bash
cd native/FloatingPanel && ./build.sh          # 产物 build/hsbgpanel.app
open build/hsbgpanel.app
HSBG_PANEL_FORCE_SHOW=1 open build/hsbgpanel.app   # 炉石没开时也显示，方便调样式
```

### 1. 沙箱里编译 Swift 必须设五个缓存变量

裸 `swiftc` 会报

```
swiftc: error: couldn't create cache file '/var/folders/.../T/xcrun_db-XXXX' (errno=Operation not permitted)
swiftc: error: permissionDenied
```

**只有这一行，看不出是哪个变量**，而且是 `swiftc` 自己的问题不是代码的问题。
必须五个**一起**设（缺任一个都照样 permissionDenied）：

```bash
export TMPDIR="$CACHE"
export SWIFT_MODULECACHE_PATH="$CACHE/modules"
export CLANG_MODULE_CACHE_PATH="$CACHE/clang"
export SWIFT_CACHE_PATH="$CACHE/swiftcache"
export XCRUN_CACHE_PATH="$CACHE/xcrun"
```

`build.sh` 已经做完了这些。另外 `swift build`（SwiftPM）在本机**始终不可用**
（构建引擎读 `.build/**/output-file-map.json` 报 I/O error），所以走 `swiftc` 直编。

### 2. 窗口跟随不需要辅助功能权限

原设计（ADR-8）打算用 Accessibility API，实测本机 `AXIsProcessTrusted()` 为 false、
查自身 AX 直接返回 `-25208 (kAXErrorAPIDisabled)`。但

```swift
CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
```

**零权限就能拿到所有窗口的 owner 名和矩形**。所以 CGWindowList 是主路径，AX 只用来
提精度，都被挡住才退回贴屏幕右缘——用户不必去开辅助功能。

⚠️ `kCGWindowBounds` 是**左上原点**，AppKit 是左下原点，要翻：
`flippedY = screenHeight - y - h`。`kCGWindowName` 才需要屏幕录制权限，owner 名不需要。

### 3. `hsbg panel` 没有 `--json` 开关

它**永远**输出 JSON Lines。`DataFeed` 一度传了 `["panel", "--json"]`，结果是
`hsbg: error: unrecognized arguments: --json`、退出码 2、无限重启，界面上只显示
"等待炉石开局…"，看起来像解析器没数据。**定位靠 `/tmp/hsbgpanel.log`**
（`DataFeed.trace()` 写的）——面板是 `LSUIElement` 后台 app，stderr 去向不可靠。

同一个文件还有第二个坑：`proc.standardError = Pipe()` 如果**没人读**，缓冲区写满后
会把子进程阻塞死。必须挂 `readabilityHandler` 持续消费。

### 4. AppKit 的 `draw(at:)` 不会裁剪

超宽的文字会**直接画到面板外面**（实测"升本"和"对手"两行被切一半）。`PanelContentView`
里自己按字宽 `clipToWidth()` 截断；商店行还要给右侧的概率留 `reserveRight`，
否则长卡名把 `P(买)` 挤出去。

### 5. 炉石窗口很宽时，右侧放不下别翻到左边

炉石 1115 pt 宽 + 屏幕 1920 pt 时，右侧只剩 139 pt。原逻辑"右边放不下就翻到左边"
会让面板跑到屏幕另一头（实测 x=310，压在浏览器上）。**正确做法是贴屏幕右缘**
（`screen.maxX - width - gap`，与游戏轻微重叠），只有屏幕右缘也放不下才考虑左侧。

### 6. ⚠️ 改窗口高度时先存 `frame.maxY`，再改 `size`

`setContentHeight()` 原本这么写：

```swift
var f = frame
f.size.height = clamped
f.origin.y = f.maxY - clamped   // ← 想"顶部对齐、向下生长"
```

但 `f.maxY` 是 `origin.y + size.height`，而 `size.height` **上一行刚被改过**，
于是式子退化成 `origin.y = 旧的 origin.y` —— 实际效果是**底边钉死、向上生长**。
实测症状：内容变多时面板一路往上爬，700 pt 高时顶到屏幕最上沿（y=30），
而炉石在 y=233，完全没对齐。

改成先 `let top = frame.maxY` 存下来，再改 size，就没这个问题了。

### 7. 分数为负也要显示

`PanelContentView` 里原本是 `if row.score > 0 { …分 }`，**负分被整段吞掉**。
实测商店里评分 -1.4 / -1.8 的行渲染成"没有分数"，看着像规则引擎没给出意见，
实际上它明确判定为"不值" —— 而这恰恰是最该让用户看到的信号。
CLI 侧一直是无条件打印的，两边口径不一致。改成 `if row.score != 0`。

---

## 测试

```bash
./run-tests          # 全部（323 个）
./run-tests -v       # 逐个打印
./run-tests -k tag   # 只跑名字含 "tag" 的
```

本机没装 pytest，所以 `tests/run_tests.py` 是自带的零依赖运行器（发现
`tests/test_*.py`、执行 `test_*` 函数）。注意 `-k` 过滤的是**测试函数名**，不是文件名。

`./hsbg check [Power.log]` 是**另一个层次**的验证：它跑真实日志，断言行数守恒与归属语义，
比如「被吸收的属性行里 `tag=ENTITY_ID` 必须等于宿主 id」——这类断言能抓住
"格式猜错了但计数刚好对上"的假通过。断言一律用**比例**而不是绝对条数，
所以 3 MB 的新会话日志和 96 MB 的参考日志都能跑到 11/11。

模型层的测试**全部离线**：prompt 构造、答案映射、概率换算都是纯函数；
HTTP 读法用一个假的 `urlopen` 桩喂"服务端原始响应"，所以 `./run-tests`
不需要模型在跑。

---

## 参考

- 完整需求、ADR、验收标准：`docs/需求分析.md`
- JevK5（本地决策模型）：<https://github.com/allebee/jevk5>
- HearthstoneJSON（卡表数据源）：<https://api.hearthstonejson.com/v1/latest/zhCN/cards.json>
