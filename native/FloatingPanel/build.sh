#!/bin/bash
#
# 构建 hsbgpanel.app
#
# ⚠️ 为什么不用 SwiftPM：
#   本机（DSH 沙箱）下 `swift build` 会在读 `.build/**/output-file-map.json` 时报 I/O error
#   —— 构建引擎内部会再调一层 sandbox-exec，嵌套沙箱被拒。直接调 `swiftc` 完全正常。
#
# ⚠️ 为什么必须手搓 .app 外壳：
#   `NSApplication` 的 `.accessory` 激活策略、以及以后可能加的 `MenuBarExtra`，
#   都依赖 bundle 标识与 Info.plist。裸可执行文件能跑，但行为会退化。
#
# ⚠️ 为什么必须重定向那几个缓存目录：
#   沙箱不允许写 /var/folders/... 与 ~/Library/Caches。不设置的话 swiftc 直接
#   报 `error: permissionDenied`（且只有这一行，看不出原因）。
#   TMPDIR 必须一起改 —— 只改 SWIFT_*/CLANG_* 不够。
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/Sources/hsbgpanel"
BUILD="$HERE/build"
CACHE="$BUILD/cache"
APP="$BUILD/hsbgpanel.app"

APP_NAME="hsbgpanel"
BUNDLE_ID="local.hsbg.panel"
VERSION="0.1.0"

# ---- 沙箱友好的编译环境 ----
mkdir -p "$CACHE/modules" "$CACHE/clang"
export TMPDIR="$CACHE"
export SWIFT_MODULECACHE_PATH="$CACHE/modules"
export CLANG_MODULE_CACHE_PATH="$CACHE/clang"
export SWIFT_CACHE_PATH="$CACHE/swiftcache"
export XCRUN_CACHE_PATH="$CACHE/xcrun"

# swiftc 在沙箱里必现一串 xcrun_db 噪声；先落到日志再过滤，便于失败时排查。
# ⚠️ 不用 `2> >(...)`：沙箱下 /dev/fd 不可用（Operation not permitted）。
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
LOG="$BUILD/build.log"
echo "==> 编译 (swiftc -O)"
set +e
swiftc \
    -O \
    -whole-module-optimization \
    -target "$(uname -m)-apple-macosx13.0" \
    -framework Cocoa \
    -framework ApplicationServices \
    -o "$APP/Contents/MacOS/$APP_NAME" \
    "$SRC"/*.swift \
    > "$LOG" 2>&1
STATUS=$?
set -e

grep -v "xcrun_db-" "$LOG" | sed '/^[[:space:]]*$/d' || true

if [ "${STATUS:-1}" -ne 0 ]; then
    echo "!! 编译失败（退出码 ${STATUS:-?}）。完整日志：$LOG"
    exit "${STATUS:-1}"
fi

echo "==> 写入 Info.plist"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>
    <string>酒馆战棋提示</string>
    <key>CFBundleExecutable</key>
    <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>CFBundleVersion</key>
    <string>$VERSION</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <!-- 不出现在 Dock / Cmd-Tab：它只是个伴随窗口 -->
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticGraphicsSwitching</key>
    <true/>
</dict>
</plist>
PLIST

printf 'APPL????' > "$APP/Contents/PkgInfo"

# 本地跑不需要签名，但 ad-hoc 签一下能避免某些 Gatekeeper 提示。
if command -v codesign >/dev/null 2>&1; then
    codesign --force --sign - "$APP" >/dev/null 2>&1 || \
        echo "    (codesign 跳过)"
fi

echo "==> 完成：$APP"
echo "    运行：open '$APP'"
echo "    或：  '$APP/Contents/MacOS/$APP_NAME'"
