"""`hsbg.cli` 的冒烟测试。

这里只放"接线错了会立刻炸"的用例 —— 命令行参数、分支变量、返回码。真正的
解析/规则逻辑在各自的测试文件里覆盖。
"""

from __future__ import annotations

import io
import sys
import tempfile
import contextlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hsbg.cli import main  # noqa: E402

_HEAD = "D 21:12:25.2665920 GameState.DebugPrintPower() - "


def _quiet(argv):
    """跑一次 CLI，吞掉它的 stdout（这些用例只关心"有没有炸"和返回码）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return main(argv)


def test_check_with_an_explicit_path_does_not_crash():
    """⚠️ 回归：`./hsbg check <path>` 曾崩在

        UnboundLocalError: local variable 'session' referenced before assignment

    因为「解析速度」那一项用的是 `session.power_log_size()`，而 `session` 只在
    **没给 path** 的分支里赋值。症状很坑：它是在打印完前 10 项之后才炸的，
    看起来像"前 10 项都通过了"。
    """
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "Power.log"
        p.write_text(_HEAD + "CREATE_GAME\n", encoding="utf-8")
        rc = _quiet(["check", str(p)])
        assert rc in (0, 1)          # 内容太少必然不合格，但**不能**崩


def test_check_without_path_on_an_empty_log_root_reports_failure():
    with tempfile.TemporaryDirectory() as d:
        rc = _quiet(["check", d])
        assert rc == 1


def test_check_accepts_a_directory_that_holds_a_power_log():
    """`./hsbg check <目录>` 要认这个目录本身就是一个会话目录。

    ⚠️ `list_sessions()` 是在**给定目录下面**找会话的，所以直接给
    `Hearthstone_2026_…/` 时它会是空的 —— 得先认 `Power.log`。
    """
    head = "D 21:12:25.2665920 GameState.DebugPrintPower() - "
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "Power.log").write_text(head + "CREATE_GAME\n", encoding="utf-8")
        rc = _quiet(["check", d])
        assert rc in (0, 1)          # 内容不足会不合格，但不能崩、也不能说"找不到"


def test_check_without_any_path_uses_the_log_root():
    """⚠️ 回归：`./hsbg check`（不给任何路径）曾崩在

        TypeError: unsupported operand type(s) for /: 'str' and 'str'

    因为 `--log-root` 的默认值是字符串，而代码直接写了 `root / POWER_LOG_NAME`。
    只在完全不给路径时才触发 —— 给了 path 的分支会自己转 `Path`，所以之前的
    用例全都没覆盖到。
    """
    with tempfile.TemporaryDirectory() as d:
        session = Path(d) / "Hearthstone_2026_01_01_00_00_00"
        session.mkdir()
        (session / "Power.log").write_text(_HEAD + "CREATE_GAME\n", encoding="utf-8")
        # ⚠️ `--log-root` 是**顶层**参数，必须写在子命令**前面**；写在后面
        # argparse 会抛 SystemExit(2)（unrecognized arguments），把测试运行器
        # 整个带崩（退出码 2、连"通过 N 失败 N"都不打印）。
        rc = _quiet(["--log-root", d, "check"])
        assert rc in (0, 1)          # 内容不足会不合格，但**不能**崩


def test_parse_limit_runs_on_a_small_log():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "Power.log"
        p.write_text(_HEAD + "CREATE_GAME\n" + _HEAD + "tag=STEP value=MAIN_ACTION\n",
                     encoding="utf-8")
        assert _quiet(["parse", str(p), "--limit", "5"]) == 0
