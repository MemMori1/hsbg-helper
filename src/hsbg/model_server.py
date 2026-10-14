#!/usr/bin/env python3
"""P4：JevK5 本地推理服务的启停。

为什么不用 LM Studio 的 HTTP 服务
---------------------------------
LM Studio 6324 端口**不返回 logprobs**（带 `n_probs`/`logprobs` 直接 400，
不带则 `logprobs: null`），而 JevK5 的置信度全靠答案位置字母的 softmax。
好在 LM Studio 自带的 llama.cpp 引擎里就有 `llama-server`，它原生支持 `n_probs`：

    ~/.cache/lm-studio/extensions/backends/llama.cpp-mac-arm64-apple-metal-advsimd-2.56.0/llama-server

本模块负责找到它、找到 GGUF、拼出启动命令，并可选地真的拉起来（`start()`）。
"""
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_PORT = 8090
DEFAULT_URL = "http://127.0.0.1:%d" % DEFAULT_PORT
DEFAULT_MODEL_DIR = Path.home() / ".cache/lm-studio/models"
DEFAULT_BACKEND_GLOB = str(
    Path.home() / ".cache/lm-studio/extensions/backends" / "*" / "llama-server")
DEFAULT_GGUF_GLOB = str(DEFAULT_MODEL_DIR / "alibiserikbay" / "JevK5-GGUF" / "*.gguf")
PID_FILE = Path("/tmp/hsbg-llama-server.pid")
LOG_FILE = Path("/tmp/hsbg-llama-server.log")
DEFAULT_CONTEXT = 8192


def _version_key(path: str) -> Tuple:
    """把后端目录名里的版本号提出来排序（找不到就给 0）。"""
    name = Path(path).parent.name
    nums: List[int] = []
    cur = ""
    for ch in name:
        if ch.isdigit() or ch == ".":
            cur += ch
        else:
            if cur:
                nums.append(cur)
                cur = ""
    if cur:
        nums.append(cur)
    try:
        return tuple(int(x) for x in ".".join(nums).split(".") if x)
    except ValueError:
        return (0,)


def find_llama_server(glob_pattern: str = DEFAULT_BACKEND_GLOB) -> Optional[str]:
    """找 LM Studio 自带的 llama-server，优先用版本号最高的那个。"""
    hits = [p for p in glob.glob(glob_pattern) if os.access(p, os.X_OK)]
    if not hits:
        return None
    hits.sort(key=_version_key, reverse=True)
    return hits[0]


def _quant_rank(low_name: str) -> int:
    """量化档位排序值，越大越好。"""
    tags = ("q8_0", "q6_k", "q5_k_m", "q4_k_m", "q4_0")
    for i, tag in enumerate(tags):
        if tag in low_name:
            return len(tags) - i
    return 0


#: 参数量档位的偏好顺序 —— **不是"越大越好"**。
#: 独立评测 JevBench v1.4 上 9B 反而不如 4B（hard tier 0.730 vs 0.784），
#: 且 9B 校准更差（ECE 0.126 vs 0.054）；官方校准温度表也给 4B 单列了值。
#: 2B 最弱（区分度不足，实测 P(买) 全挤在 0.42~0.48），只作 4B 缺失时的退路。
_SIZE_PREFERENCE = ("4b", "2b", "9b")


def _size_rank(low_name: str) -> int:
    """按 `_SIZE_PREFERENCE` 给参数量档位打分；认不出给 0（排最后）。"""
    for i, tag in enumerate(_SIZE_PREFERENCE):
        if re.search(r"(?:^|[-_/])%s(?:[-_.]|$)" % tag, low_name):
            return len(_SIZE_PREFERENCE) - i
    return 0


def size_key(gguf_path: str) -> str:
    """从 GGUF 文件名推出校准温度表的档位键，如 `"4b-v0.2"`。

    文件名形如 `jevk5-4b-v0.2-Q4_K_M.gguf`。版本号必须带上：4B 的 v0.2 与 v0.3
    校准温度不同（1.532 vs 1.367），认错版本会让概率整体偏平或偏尖。
    """
    low = os.path.basename(gguf_path).lower()
    m_size = re.search(r"(?:^|[-_/])(\d+b)(?:[-_.]|$)", low)
    if not m_size:
        return ""
    size = m_size.group(1)
    m_ver = re.search(r"[-_](v\d+\.\d+)", low)
    return "%s-%s" % (size, m_ver.group(1)) if m_ver else size


def find_gguf(glob_pattern: str = DEFAULT_GGUF_GLOB) -> Optional[str]:
    """找 JevK5 的 GGUF。

    先按参数量档位偏好（4B > 2B > 9B，见 `_SIZE_PREFERENCE` 里为什么不是越大越好），
    同档位内再挑量化最好的（Q8_0 > Q6_K > Q5_K_M > Q4_K_M > Q4_0），最后才看文件大小。
    """
    hits = glob.glob(glob_pattern)
    if not hits:
        return None

    def rank(p: str) -> Tuple:
        low = os.path.basename(p).lower()
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        return (_size_rank(low), _quant_rank(low), size)

    hits.sort(key=rank, reverse=True)
    return hits[0]


def serve_command(server: Optional[str] = None, model: Optional[str] = None,
                  port: int = DEFAULT_PORT, context: int = DEFAULT_CONTEXT,
                  gpu_layers: int = 99) -> str:
    """拼出可直接粘贴运行的启动命令。"""
    s = server or find_llama_server() or "<llama-server 路径>"
    m = model or find_gguf() or "<jevk5-*.gguf 路径>"
    return ('"%s" -m "%s" -c %d -ngl %d --port %d --host 127.0.0.1'
            % (s, m, context, gpu_layers, port))


def health(url: str = DEFAULT_URL, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return json.loads(r.read()).get("status") == "ok"
    except (urllib.error.URLError, ValueError, OSError):
        return False


def wait_ready(url: str = DEFAULT_URL, seconds: float = 90.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if health(url):
            return True
        time.sleep(0.5)
    return False


def read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def start(server: Optional[str] = None, model: Optional[str] = None,
          port: int = DEFAULT_PORT, context: int = DEFAULT_CONTEXT,
          url: Optional[str] = None, wait: float = 90.0) -> int:
    """后台拉起 llama-server。已在跑就直接返回 0。"""
    url = url or ("http://127.0.0.1:%d" % port)
    if health(url):
        return 0
    old = read_pid()
    if old is not None and is_alive(old):
        print("已有 hsbg 的 llama-server 在跑（pid %d），等它就绪…" % old)
        return 0 if wait_ready(url, wait) else 1
    s = server or find_llama_server()
    m = model or find_gguf()
    if not s:
        print("找不到 llama-server。装一个 LM Studio 或 llama.cpp，"
              "或用 --model-url 指向别处。", file=sys.stderr)
        return 2
    if not m:
        print("找不到 JevK5 GGUF（%s）" % DEFAULT_GGUF_GLOB, file=sys.stderr)
        return 2
    args = [s, "-m", m, "-c", str(context), "-ngl", "99",
            "--port", str(port), "--host", "127.0.0.1"]
    with open(LOG_FILE, "ab") as log:
        p = subprocess.Popen(args, stdout=log, stderr=log,
                             start_new_session=True)
    PID_FILE.write_text(str(p.pid))
    print("已启动 llama-server（pid %d，日志 %s）" % (p.pid, LOG_FILE))
    if wait_ready(url, wait):
        print("✅ 就绪：%s" % url)
        return 0
    print("⚠️ %ds 内没就绪，看日志：%s" % (int(wait), LOG_FILE), file=sys.stderr)
    return 1


def stop() -> int:
    pid = read_pid()
    if pid is None:
        print("没有记录的 pid（%s）" % PID_FILE)
        return 0
    if not is_alive(pid):
        print("进程 %d 已不在" % pid)
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if not is_alive(pid):
            break
        time.sleep(0.25)
    else:
        os.kill(pid, signal.SIGKILL)
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    print("已停止 llama-server（pid %d）" % pid)
    return 0
