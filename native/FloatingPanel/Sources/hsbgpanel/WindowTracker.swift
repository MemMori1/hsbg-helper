// WindowTracker.swift
//
// 找到炉石窗口的位置，并跟踪它的移动/缩放。
//
// 两级取几何（重要）：
//   1. Accessibility API（`AXUIElement`）—— 精确，但需要用户授权辅助功能权限。
//   2. `CGWindowListCopyWindowInfo` —— **不需要任何权限**，能拿到 owner 名与矩形。
//      ⚠️ 实测：本机 `AXIsProcessTrusted()` 为 false、AX 查询返回 -25208
//      (`kAXErrorAPIDisabled`)，而 CGWindowList 立刻就能拿到所有普通窗口的矩形。
//      所以 CGWindowList 不是"降级方案"，而是**默认可用的主路径**。
//   3. 都拿不到 → 退回屏幕右缘（`DockGeometry`）。

import Cocoa
import ApplicationServices

/// 炉石窗口的几何信息（屏幕坐标，原点左下 —— 与 AppKit 一致）。
struct TargetWindow {
    var rect: CGRect
    var pid: pid_t
    var ownerName: String
    var via: Source

    enum Source: String {
        case accessibility = "AX"
        case windowList = "CGWindowList"
        case screenEdge = "屏幕右缘"
    }

    var midX: CGFloat { rect.midX }
    var maxY: CGFloat { rect.maxY }
}

protocol WindowTrackerDelegate: AnyObject {
    /// 目标窗口几何发生了变化（或被找到/丢失）。
    func windowTracker(_ tracker: WindowTracker, didUpdate target: TargetWindow?)
}

final class WindowTracker {
    /// 炉石在这个系统上的进程名候选（中英文都列上）。
    static let ownerCandidates = ["Hearthstone", "炉石传说", "Hearthstone Helper"]

    /// 小于这个尺寸的窗口不是游戏主窗口（排除弹窗、提示框）。
    static let minWindowSize = CGSize(width: 400, height: 300)

    weak var delegate: WindowTrackerDelegate?

    /// 轮询间隔。CGWindowList 很便宜（微秒级），10 Hz 足够跟手；
    /// 有 AX 通知时这个轮询只作为兜底。
    var pollInterval: TimeInterval = 0.1

    private var timer: Timer?
    private var axObserver: AXObserver?
    private var axAppElement: AXUIElement?
    private var lastTarget: TargetWindow?
    /// 记住上一次探测结果，避免每轮都重新给用户弹权限提示。
    private(set) var accessibilityTrusted: Bool = false
    private(set) var lastSource: TargetWindow.Source = .screenEdge

    func start() {
        accessibilityTrusted = AXIsProcessTrusted()
        if accessibilityTrusted {
            installAXObserver()
        }
        // 无论有没有 AX，都用轮询兜底：AX 通知在窗口缩放时可能漏发。
        let t = Timer(timeInterval: pollInterval, repeats: true) { [weak self] _ in
            self?.poll()
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        poll()
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        if let obs = axObserver, let app = axAppElement {
            CFRunLoopRemoveSource(CFRunLoopGetCurrent(),
                                  AXObserverGetRunLoopSource(obs),
                                  .defaultMode)
            _ = app
        }
        axObserver = nil
        axAppElement = nil
    }

    // MARK: - 探测

    @discardableResult
    func poll() -> TargetWindow? {
        let target = findHearthstoneWindow()
        let changed = !sameTarget(target, lastTarget)
        lastTarget = target
        if changed {
            lastSource = target?.via ?? .screenEdge
            delegate?.windowTracker(self, didUpdate: target)
        }
        return target
    }

    /// 真的不一样才算变化（避免每 100 ms 都重绘）。
    private func sameTarget(_ a: TargetWindow?, _ b: TargetWindow?) -> Bool {
        switch (a, b) {
        case (nil, nil): return true
        case let (l?, r?):
            return l.pid == r.pid && abs(l.rect.minX - r.rect.minX) < 0.5
                && abs(l.rect.minY - r.rect.minY) < 0.5
                && abs(l.rect.width - r.rect.width) < 0.5
                && abs(l.rect.height - r.rect.height) < 0.5
        default: return false
        }
    }

    // MARK: - 路径 2：CGWindowList（无需权限）

    /// 从 CGWindowList 里挑出炉石主窗口。
    /// ⚠️ `kCGWindowBounds` 用的是**左上原点**的屏幕坐标，AppKit 是左下原点，
    /// 需要用主屏高度翻一次。
    private func findViaWindowList() -> TargetWindow? {
        let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
        guard let raw = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
            return nil
        }
        let screenHeight = NSScreen.screens.first?.frame.height ?? 0

        var best: TargetWindow?
        var bestArea: CGFloat = 0
        for w in raw {
            // 只要普通窗口层（layer 0）——排除菜单栏、悬浮提示、Dock。
            let layer = w[kCGWindowLayer as String] as? Int ?? -1
            guard layer == 0 else { continue }
            let owner = w[kCGWindowOwnerName as String] as? String ?? ""
            guard Self.ownerCandidates.contains(where: { owner.localizedCaseInsensitiveContains($0) })
            else { continue }
            guard let bounds = w[kCGWindowBounds as String] as? [String: Any],
                  let x = bounds["X"] as? Double, let y = bounds["Y"] as? Double,
                  let bw = bounds["Width"] as? Double, let bh = bounds["Height"] as? Double
            else { continue }
            guard bw >= Double(Self.minWindowSize.width), bh >= Double(Self.minWindowSize.height)
            else { continue }

            let area = CGFloat(bw * bh)
            guard area > bestArea else { continue }
            bestArea = area
            // ⚠️ 左上原点 → 左下原点
            let flippedY = screenHeight - CGFloat(y) - CGFloat(bh)
            best = TargetWindow(
                rect: CGRect(x: CGFloat(x), y: flippedY, width: CGFloat(bw), height: CGFloat(bh)),
                pid: pid_t(w[kCGWindowOwnerPID as String] as? Int ?? -1),
                ownerName: owner,
                via: .windowList)
        }
        return best
    }

    // MARK: - 路径 1：Accessibility（需要授权）

    private func hearthstonePID() -> pid_t? {
        // 先靠 CGWindowList 找到 pid，再用 AX 读更精确的窗口（AX 能拿到
        // 不受 Dock/菜单栏影响的"内容区"尺寸）。
        findViaWindowList()?.pid
    }

    private func findViaAccessibility() -> TargetWindow? {
        guard accessibilityTrusted, let pid = hearthstonePID(), pid > 0 else { return nil }
        let app = AXUIElementCreateApplication(pid)
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &value) == .success,
              let windows = value as? [AXUIElement]
        else { return nil }

        var best: TargetWindow?
        var bestArea: CGFloat = 0
        for win in windows {
            guard let rect = axRect(of: win) else { continue }
            guard rect.width >= Self.minWindowSize.width,
                  rect.height >= Self.minWindowSize.height else { continue }
            let area = rect.width * rect.height
            guard area > bestArea else { continue }
            bestArea = area
            best = TargetWindow(rect: rect, pid: pid, ownerName: "Hearthstone", via: .accessibility)
        }
        return best
    }

    private func axRect(of element: AXUIElement) -> CGRect? {
        var posRef: CFTypeRef?
        var sizeRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &posRef) == .success,
              AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &sizeRef) == .success
        else { return nil }
        var origin = CGPoint.zero
        var size = CGSize.zero
        // swiftlint:disable:next force_cast
        AXValueGetValue(posRef as! AXValue, .cgPoint, &origin)
        AXValueGetValue(sizeRef as! AXValue, .cgSize, &size)
        guard size.width > 0, size.height > 0 else { return nil }
        return CGRect(origin: origin, size: size)
    }

    // MARK: - 路径选择

    func findHearthstoneWindow() -> TargetWindow? {
        if let t = findViaAccessibility() { return t }
        return findViaWindowList()
    }

    // MARK: - AX 通知（有权限时更跟手）

    private func installAXObserver() {
        guard let pid = hearthstonePID() ?? findHearthstonePIDViaNSWorkspace(), pid > 0 else { return }
        var observer: AXObserver?
        let callback: AXObserverCallback = { _, _, _, refcon in
            guard let refcon = refcon else { return }
            let tracker = Unmanaged<WindowTracker>.fromOpaque(refcon).takeUnretainedValue()
            // 通知可能在非主线程发出，切回主线程。
            DispatchQueue.main.async { tracker.poll() }
        }
        let err = AXObserverCreate(pid, callback, &observer)
        guard err == .success, let obs = observer else { return }
        let app = AXUIElementCreateApplication(pid)
        let selfPtr = Unmanaged.passUnretained(self).toOpaque()
        for note in [kAXWindowMovedNotification, kAXWindowResizedNotification,
                     kAXFocusedWindowChangedNotification] {
            AXObserverAddNotification(obs, app, note as CFString, selfPtr)
        }
        CFRunLoopAddSource(CFRunLoopGetCurrent(), AXObserverGetRunLoopSource(obs), .defaultMode)
        axObserver = obs
        axAppElement = app
    }

    private func findHearthstonePIDViaNSWorkspace() -> pid_t? {
        for app in NSWorkspace.shared.runningApplications {
            guard let name = app.localizedName else { continue }
            if Self.ownerCandidates.contains(where: { name.localizedCaseInsensitiveContains($0) }) {
                return app.processIdentifier
            }
        }
        return nil
    }

    /// 请求辅助功能权限（会弹系统对话框）。只在用户主动点"提升精度"时调用。
    @discardableResult
    func requestAccessibilityPermission() -> Bool {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        let granted = AXIsProcessTrustedWithOptions(opts)
        accessibilityTrusted = granted
        if granted { installAXObserver() }
        return granted
    }
}
