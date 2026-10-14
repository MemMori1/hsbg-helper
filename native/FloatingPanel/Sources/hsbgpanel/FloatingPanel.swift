// FloatingPanel.swift
//
// 吸附在炉石右侧的悬浮框。
//
// ADR-9 的三个关键设置，缺一个都会出问题：
//   - `NSPanel(styleMask: [.nonactivatingPanel, ...])`：点悬浮框不抢炉石焦点
//   - `level = .floating`：永远压在炉石上面
//   - `collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]`：
//     ⚠️ 炉石全屏时窗口在**独立的 Space** 里，少这两个标志悬浮框直接不可见

import Cocoa

/// 悬浮框几何计算：决定贴在炉石的哪一侧、放不下时怎么降级。
enum DockGeometry {
    /// 面板与炉石窗口之间的间隙。
    static let gap: CGFloat = 8
    /// 用户按住拖动后记住的偏移（相对默认贴合位置）。
    struct Offset: Equatable {
        var dx: CGFloat = 0
        var dy: CGFloat = 0
    }

    /// 在给定屏幕几何下，算出面板应放的位置。
    /// - Parameters:
    ///   - panelSize: 面板尺寸
    ///   - target: 炉石窗口矩形（屏幕坐标，左下原点）
    ///   - screen: 炉石所在屏幕的可见区域（已排除菜单栏/Dock）
    ///   - offset: 用户拖出来的偏移
    /// - Returns: 面板原点，以及实际贴的是哪一侧
    static func dock(panelSize: CGSize,
                     target: CGRect?,
                     screen: CGRect,
                     offset: Offset = Offset()) -> (origin: CGPoint, side: Side) {
        guard let target = target else {
            // 没有目标：贴屏幕右缘，垂直居中。
            return (CGPoint(x: screen.maxX - panelSize.width - gap,
                            y: screen.midY - panelSize.height / 2 + offset.dy), .right)
        }

        let topAlignedY = target.maxY - panelSize.height
        let rightOrigin = CGPoint(x: target.maxX + gap, y: topAlignedY)
        let fitsRight = rightOrigin.x + panelSize.width <= screen.maxX

        if fitsRight {
            return (CGPoint(x: rightOrigin.x + offset.dx,
                            y: clampedY(topAlignedY + offset.dy, panelSize: panelSize, screen: screen)),
                    .right)
        }

        // ⚠️ 右侧被炉石占满时**不要翻到左边**。
        // 实测炉石窗口 1115pt 宽时右侧只剩 139pt，翻左会把面板压到 x=310 处
        // （浏览器/桌面上），既挡视野又完全不在游戏旁边。用户要的是"贴右边"，
        // 所以宁可与游戏右缘轻微重叠，也留在右侧；只有右缘也放不下（面板比屏幕还宽）
        // 才考虑左侧。
        let edgeOrigin = CGPoint(x: screen.maxX - panelSize.width - gap, y: topAlignedY)
        if edgeOrigin.x + panelSize.width > screen.minX {
            return (CGPoint(x: max(screen.minX, edgeOrigin.x + offset.dx),
                            y: clampedY(topAlignedY + offset.dy, panelSize: panelSize, screen: screen)),
                    .right)
        }

        let leftOrigin = CGPoint(x: target.minX - gap - panelSize.width, y: topAlignedY)
        let fitsLeft = leftOrigin.x >= screen.minX
        if fitsLeft {
            return (CGPoint(x: leftOrigin.x + offset.dx,
                            y: clampedY(topAlignedY + offset.dy, panelSize: panelSize, screen: screen)),
                    .left)
        }

        // 两边都放不下（炉石铺满整屏）：叠在右缘、略微内缩。
        let overlayX = min(target.maxX - panelSize.width - gap, screen.maxX - panelSize.width - gap)
        return (CGPoint(x: max(screen.minX, overlayX + offset.dx),
                        y: clampedY(topAlignedY + offset.dy, panelSize: panelSize, screen: screen)),
                .overlay)
    }

    private static func clampedY(_ y: CGFloat, panelSize: CGSize, screen: CGRect) -> CGFloat {
        let minY = screen.minY
        let maxY = screen.maxY - panelSize.height
        guard maxY > minY else { return minY }
        return min(max(y, minY), maxY)
    }

    enum Side: String {
        case right, left, overlay
    }
}

protocol FloatingPanelDelegate: AnyObject {
    /// 用户拖动了面板（或点了"贴回"），需要持久化新的偏移。
    func floatingPanelDidChangeOffset(_ panel: FloatingPanel, offset: DockGeometry.Offset)
    /// 点「升级精度」按钮。
    func floatingPanelDidRequestAccessibility(_ panel: FloatingPanel)
}

final class FloatingPanel: NSPanel {
    weak var panelDelegate: FloatingPanelDelegate?

    private let content = PanelContentView()

    /// 默认宽度。炉石窗口化时右侧通常有 640–1160 pt 余量，
    /// 340 pt 够放"商店 N 项 + 逐张理由"。
    static let defaultWidth: CGFloat = 340
    /// 面板高度自适应内容，但夹在这个区间内。
    static let minHeight: CGFloat = 160
    static let maxHeight: CGFloat = 900

    private var userOffset = DockGeometry.Offset()
    private var dragOrigin: CGPoint?
    private var isProgrammaticMove = false

    init() {
        super.init(contentRect: CGRect(x: 0, y: 0, width: Self.defaultWidth, height: 320),
                   styleMask: [.nonactivatingPanel, .titled, .fullSizeContentView],
                   backing: .buffered,
                   defer: false)

        isFloatingPanel = true
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        titleVisibility = .hidden
        titlebarAppearsTransparent = true
        isMovableByWindowBackground = true
        hidesOnDeactivate = false
        becomesKeyOnlyIfNeeded = true
        backgroundColor = .clear
        isOpaque = false
        hasShadow = true
        standardWindowButton(.closeButton)?.isHidden = true
        standardWindowButton(.miniaturizeButton)?.isHidden = true
        standardWindowButton(.zoomButton)?.isHidden = true

        contentView = content
        content.onMoveBy = { [weak self] dx, dy in
            self?.nudge(dx: dx, dy: dy)
        }
        content.onReset = { [weak self] in
            guard let self = self else { return }
            self.userOffset = DockGeometry.Offset()
            self.panelDelegate?.floatingPanelDidChangeOffset(self, offset: self.userOffset)
            self.reposition()
            self.content.flashStatus("已贴回炉石右侧")
        }
        content.onRequestAccessibility = { [weak self] in
            guard let self = self else { return }
            self.panelDelegate?.floatingPanelDidRequestAccessibility(self)
        }
    }

    /// 悬浮框不应该抢走炉石的键盘焦点 —— 点它只为了看，不是去操作。
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    /// 换个初始位置（还没找到炉石时的落脚点）。
    func placeInitial(screen: CGRect) {
        let size = frame.size
        setFrameOrigin(CGPoint(x: screen.maxX - size.width - DockGeometry.gap,
                               y: screen.midY - size.height / 2))
    }

    /// 把面板贴回炉石右侧（或降级到左/屏幕右缘）。
    /// - Parameter target: 炉石窗口矩形；nil 表示炉石不在前台/没找到。
    func reposition(target: CGRect? = nil) {
        let screen = screenFor(target: target)
        let size = frame.size
        let docked = DockGeometry.dock(panelSize: size, target: target,
                                       screen: screen, offset: userOffset)
        isProgrammaticMove = true
        setFrameOrigin(docked.origin)
        isProgrammaticMove = false
        content.setSide(docked.side, targetVisible: target != nil)
    }

    private func screenFor(target: CGRect?) -> CGRect {
        guard let target = target else {
            return (screen ?? NSScreen.main)?.visibleFrame ?? CGRect(x: 0, y: 0, width: 1920, height: 1080)
        }
        // 取与炉石窗口重叠面积最大的屏幕。
        var best: NSScreen?
        var bestArea: CGFloat = 0
        for s in NSScreen.screens {
            let inter = s.frame.intersection(target)
            guard !inter.isNull else { continue }
            let area = inter.width * inter.height
            if area > bestArea { bestArea = area; best = s }
        }
        return (best ?? screen ?? NSScreen.main)?.visibleFrame
            ?? CGRect(x: 0, y: 0, width: 1920, height: 1080)
    }

    /// 用户按住拖动：累计成偏移，而不是直接改窗口位置 ——
    /// 这样下一次 reposition() 仍然贴着炉石。
    private func nudge(dx: CGFloat, dy: CGFloat) {
        userOffset.dx += dx
        userOffset.dy += dy
        panelDelegate?.floatingPanelDidChangeOffset(self, offset: userOffset)
        reposition()
    }

    func resetOffset() {
        userOffset = DockGeometry.Offset()
        reposition()
    }

    /// 内容变了要跟着调整高度，但不能盖住炉石。
    func setContentHeight(_ height: CGFloat) {
        let clamped = min(max(height, Self.minHeight), Self.maxHeight)
        guard abs(clamped - frame.height) > 0.5 else { return }
        // ⚠️ 必须先记下旧的 maxY（AppKit 里 = 窗口**上边缘**）再改 size。
        // 早先写成先 `f.size.height = clamped` 再 `f.origin.y = f.maxY - clamped`，
        // 而 `f.maxY` 此时已经用的是**新高度**，整条式子退化成
        // `origin.y = 旧 origin.y` —— 于是窗口是"底边钉死、向上长"。
        // 实测症状：内容变多时面板一路往上爬，700pt 高时顶到屏幕最上沿
        // （CGWindow y=30），而炉石在 y=233，完全没对齐。
        let top = frame.maxY
        var f = frame
        f.size.height = clamped
        f.origin.y = top - clamped // 顶部对齐，向下生长
        isProgrammaticMove = true
        setFrame(f, display: true)
        isProgrammaticMove = false
    }

    func update(contentState: PanelContentState) {
        content.apply(contentState)
        setContentHeight(content.fittingHeight(width: frame.width))
    }

    /// 辅助功能权限变了（只在用户右键主动开启后调用）。
    func updateAccessibilityState(_ granted: Bool) {
        content.flashStatus(granted ? "已启用辅助功能，跟随精度提升" : "未启用辅助功能，继续用窗口列表跟随")
        reposition()
    }

    /// 炉石没在前台时自动隐藏（FR-6.0）。
    func setGameActive(_ active: Bool) {
        // 调试用：`HSBG_PANEL_FORCE_SHOW=1` 时无视"炉石是否在前台"，
        // 面板始终可见可拖动。没有这个开关就没法在炉石未运行时排查渲染问题
        // （默认行为是 alpha=0，等于什么都看不见）。
        if FloatingPanel.forceShow {
            alphaValue = 1.0
            ignoresMouseEvents = false
            return
        }
        alphaValue = active ? 1.0 : 0.0
        ignoresMouseEvents = !active
    }

    /// 由环境变量 `HSBG_PANEL_FORCE_SHOW` 控制，只影响可见性判断。
    static let forceShow: Bool = {
        let v = ProcessInfo.processInfo.environment["HSBG_PANEL_FORCE_SHOW"] ?? ""
        return v == "1" || v.lowercased() == "true"
    }()
}
