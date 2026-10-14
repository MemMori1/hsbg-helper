#!/usr/bin/env python3
"""极简测试运行器（零依赖）。

为什么不用 pytest：本机没装（`/usr/bin/python3` 只有 `requests`），
而项目原则是零第三方依赖。这里只做三件事：发现 `test_*.py`、
执行里面的 `test_*` 函数、汇总结果。

用法::

    cd hsbg-helper && ./run-tests
    cd hsbg-helper && PYTHONPATH=src ./run-tests -v
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import sys
import time
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser(prog="run-tests")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-k", "--filter", help="只跑名字包含该子串的测试")
    args = ap.parse_args()

    files = sorted(TESTS_DIR.glob("test_*.py"))
    if not files:
        print("没有找到测试文件")
        return 1

    passed = failed = skipped = 0
    failures = []

    for path in files:
        mod = load_module(path)
        tests = [
            (name, fn)
            for name, fn in vars(mod).items()
            if name.startswith("test_") and callable(fn)
        ]
        tests.sort(key=lambda kv: inspect.getsourcelines(kv[1])[1])
        if not tests:
            continue
        print(f"\n{path.name}")
        for name, fn in tests:
            if args.filter and args.filter not in name:
                continue
            label = f"  {name}"
            t0 = time.time()
            try:
                fn()
            except Exception:
                failed += 1
                failures.append((path.name, name, traceback.format_exc()))
                print(f"{label} … \033[31mFAIL\033[0m")
            else:
                dt = (time.time() - t0) * 1000
                passed += 1
                if args.verbose:
                    print(f"{label} … \033[32mok\033[0m ({dt:.0f} ms)")
                else:
                    print(f"{label} … \033[32mok\033[0m")

    print(f"\n{'─' * 52}")
    if failures:
        for fname, tname, tb in failures:
            print(f"\n\033[31mFAIL {fname}::{tname}\033[0m\n{tb}")
    print(f"通过 {passed}  失败 {failed}  跳过 {skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
