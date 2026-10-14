// main.swift
//
// hsbgpanel —— 吸附在炉石传说右侧的辅助提示悬浮框。
//
// 红线（需求文档 §2.3）：**只能"看"不能"动"**。
// 这个进程只读日志、只画窗口，不注入事件、不模拟鼠标键盘。

import Cocoa

final class AppDelegate: NSObject, NSApplicationDelegate,
                         WindowTrackerDelegate, DataFeedDelegate,
                         FloatingPanelDelegate {

    private var panel: FloatingPanel!
    private var tracker: WindowTracker!
    private var feed: DataFeed!

    /// 记住用户拖出来的偏移（相对默认贴合位置）。
    private var offset = DockGeometry.Offset()
    private var lastTarget: CGRect?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // .accessory = 不出现在 Dock、不抢焦点（MenuBarExtra 依赖 bundle 标识，
        // ⚠️ 所以这个二进制必须放进 .app 外壳里跑）。
        NSApp.setActivationPolicy(.accessory)

        let screenFrame = (NSScreen.main?.visibleFrame)
            ?? CGRect(x: 0, y: 0, width: 1920, height: 1080)

        panel = FloatingPanel()
        panel.panelDelegate = self
        panel.placeInitial(screen: screenFrame)
        panel.orderFrontRegardless()   // 不用 makeKeyAndOrderFront：那会抢焦点

        tracker = WindowTracker()
        tracker.delegate = self
        tracker.start()

        feed = DataFeed()
        feed.delegate = self
        feed.start()

        // 轮询窗口位置（tracker 有变化才回调），顺便判断炉石是否在前台。
        let t = Timer(timeInterval: 0.2, repeats: true) { [weak self] _ in
            self?.syncForegroundState()
        }
        RunLoop.main.add(t, forMode: .common)
    }

    func applicationWillTerminate(_ notification: Notification) {
        feed?.stop()
        tracker?.stop()
    }

    /// 退出时把悬浮框一起收掉（FR-6.0 "退出时隐藏"）。
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    // MARK: - WindowTrackerDelegate

    func windowTracker(_ tracker: WindowTracker, didUpdate target: TargetWindow?) {
        lastTarget = target?.rect
        panel.reposition(target: target?.rect)
        syncForegroundState()
    }

    /// 炉石不在前台（或被最小化）时把悬浮框淡出，不干扰别的应用。
    private func syncForegroundState() {
        let front = NSWorkspace.shared.frontmostApplication
        let frontName = front?.localizedName ?? ""
        let isGame = WindowTracker.ownerCandidates.contains {
            frontName.localizedCaseInsensitiveContains($0)
        }
        let hasWindow = lastTarget != nil
        panel.setGameActive(isGame && hasWindow)
    }

    // MARK: - DataFeedDelegate

    func dataFeed(_ feed: DataFeed, didReceive state: PanelContentState) {
        var s = state
        s.targetVisible = lastTarget != nil
        s.source = tracker.lastSource.rawValue
        panel.update(contentState: s)
    }

    func dataFeed(_ feed: DataFeed, didFail message: String) {
        var s = PanelContentState()
        s.error = message
        s.headline = "数据源未就绪"
        s.modelNote = message
        panel.update(contentState: s)
    }

    // MARK: - FloatingPanelDelegate

    func floatingPanelDidChangeOffset(_ panel: FloatingPanel, offset: DockGeometry.Offset) {
        self.offset = offset
        panel.reposition(target: lastTarget)
    }

    func floatingPanelDidRequestAccessibility(_ panel: FloatingPanel) {
        // ⚠️ 只是提升"跟随精度"用；不授权也能工作（走 CGWindowList）。
        let granted = tracker.requestAccessibilityPermission()
        panel.updateAccessibilityState(granted)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
