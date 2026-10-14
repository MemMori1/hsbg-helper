"""炉石传说·酒馆战棋 辅助提示器。

模块划分（对应需求文档 §7 ADR-1 的四层解耦）：

    logs.py      ①  日志发现：定位最新会话目录与 Power.log
    tailer.py    ①  日志采集：增量 tail、处理轮换/游戏重启
    parser.py    ②  状态解析：把 Power.log 载荷解析成事件流
    events.py    ②  状态解析：事件模型定义
    (P1) state.py    状态重建：事件流 → GameState 快照
    (P3) rules.py    规则引擎：硬指标（不依赖模型）
    (P4) advisor.py  模型顾问：JevK5 判断题
    (P6) overlay     展示层：Swift 悬浮框（吸附炉石右侧）

本包在 P0 阶段只实现 ①②。
"""

__version__ = "0.1.0"
