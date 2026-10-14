// DecisionPanel.swift
//
// 「决策框」—— 贴在分析框（FloatingPanel）**上方**的独立小窗。
//
// 为什么单独一个窗：分析框回答的是"为什么"，决策框回答的是"现在做什么"。
// 两者混在一屏时，用户还是得自己权衡（实测过：同一屏里标题说"建议买 X"、
// 升本栏说"升本"，两个结论互相打架）。所以决策框只给**一个动作**，
// 最多一行理由，字号放大到一眼可读。
//
// 姿态与 FloatingPanel 完全一致（ADR-9）：非激活、不抢焦点、跟随炉石。

import Cocoa

/// 决策框的展示数据。对应 Python 侧 `PanelFrame.decision`。
struct DecisionState {
    var action: String = ""       // 选择 / 升本 / 买 / 刷新 / 攒钱 / 观望
    var headline: String = ""     // 大字，如 "升本 2本"
    var costText: String = ""     // "4 金"
    var reason: String = ""       // 一行理由
    var urgency: String = "soon"  // now / soon / wait
    var hasContent: Bool = false

    /// 借 `PanelFrame["decision"]` 建。
    init() {}

    init(json: [String: Any]) {
        action = json["action"] as? String ?? ""
        headline = json["headline"] as? String ?? ""
        costText = json["cost_text"] as? String ?? ""
        reason = json["reason"] as? String ?? ""
        urgency = json["urgency"] as? String ?? "soon"
        hasContent = true
        if headline.isEmpty { headline = action }
    }
}

final class DecisionContentView: NSView {
    var state = DecisionState()

    private let padding: CGFloat = 12

    private let bigFont = NSFont.systemFont(ofSize: 24, weight: .bold)
    private let costFont = NSFont.systemFont(ofSize: 15, weight: .semibold)
    private let reasonFont = NSFont.systemFont(ofSize: 11.5, weight: .regular)

    override var isFlipped: Bool { true }

    func apply(_ s: DecisionState) {
        state = s
        needsDisplay = true
    }

    /// 决策框固定两行以内，高度直接算出来。
    func fittingHeight(width: CGFloat) -> CGFloat {
        let big: CGFloat = 31
        let reason: CGFloat = state.reason.isEmpty ? 0 : 17
        return padding + big + reason + padding
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.clear.set()
        dirtyRect.fill()

        let bg = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5),
                              xRadius: 10, yRadius: 10)
        // 决策框比分析框更亮一点，视觉上是"最上面那一层"
        NSColor(calibratedWhite: 0.10, alpha: 0.96).setFill()
        bg.fill()
        accent.setStroke()
        bg.lineWidth = 1.5
        bg.stroke()

        if !state.hasContent {
            draw("等待炉石开局…", at: padding, padding + 6,
                 color: NSColor(calibratedWhite: 0.5, alpha: 1), font: reasonFont)
            return
        }

        // 动作大字
        draw(state.headline, at: padding, padding,
             color: accent, font: bigFont, reserveRight: costWidth() + 8)

        // 花费贴右
        if !state.costText.isEmpty {
            let w = textWidth(state.costText, font: costFont)
            draw(state.costText, at: bounds.width - padding - w, padding + 8,
                 color: NSColor(calibratedRed: 1.0, green: 0.84, blue: 0.4, alpha: 1),
                 font: costFont)
        }

        // 理由
        if !state.reason.isEmpty {
            draw(state.reason, at: padding, padding + 33,
                 color: NSColor(calibratedWhite: 0.72, alpha: 1), font: reasonFont)
        }
    }

    /// 按动作上色：该做的动作给暖色，等待类给灰。
    private var accent: NSColor {
        if state.urgency == "now" {
            return NSColor(calibratedRed: 1.0, green: 0.84, blue: 0.4, alpha: 1)
        }
        switch state.action {
        case "升本":
            return NSColor(calibratedRed: 0.5, green: 0.95, blue: 0.6, alpha: 1)
        case "买":
            return NSColor(calibratedRed: 0.62, green: 0.80, blue: 1.0, alpha: 1)
        case "刷新":
            return NSColor(calibratedRed: 0.72, green: 0.82, blue: 0.95, alpha: 1)
        default:
            return NSColor(calibratedWhite: 0.74, alpha: 1)
        }
    }

    private func costWidth() -> CGFloat {
        state.costText.isEmpty ? 0 : textWidth(state.costText, font: costFont)
    }

    private func textWidth(_ text: String, font: NSFont) -> CGFloat {
        (text as NSString).size(withAttributes: [.font: font]).width
    }

    private func draw(_ text: String, at x: CGFloat, _ y: CGFloat,
                      color: NSColor, font: NSFont? = nil,
                      reserveRight: CGFloat = 0) {
        let f = font ?? reasonFont
        // ⚠️ AppKit 的 `draw(at:)` 不裁剪，超宽会画到窗口外，必须自己截。
        let avail = bounds.width - x - padding - reserveRight
        let clipped = clip(text, maxWidth: avail, font: f)
        clipped.draw(at: NSPoint(x: x, y: y),
                     withAttributes: [.font: f, .foregroundColor: color])
    }

    private func clip(_ text: String, maxWidth: CGFloat, font: NSFont) -> String {
        guard maxWidth > 8 else { return "" }
        if textWidth(text, font: font) <= maxWidth { return text }
        let ellipsis = "…"
        let ellipsisWidth = textWidth(ellipsis, font: font)
        var out = ""
        var width: CGFloat = 0
        for ch in text {
            let w = textWidth(String(ch), font: font)
            if width + w + ellipsisWidth > maxWidth { break }
            out.append(ch)
            width += w
        }
        return out + ellipsis
    }
}

final class DecisionPanel: NSPanel {
    private let content = DecisionContentView()

    /// 与分析框同宽，两块叠在一起时左右对齐。
    static let panelWidth: CGFloat = 340

    init() {
        super.init(contentRect: CGRect(x: 0, y: 0, width: Self.panelWidth, height: 60),
                   styleMask: [.nonactivatingPanel, .titled, .fullSizeContentView],
                   backing: .buffered,
                   defer: false)

        isFloatingPanel = true
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        titleVisibility = .hidden
        titlebarAppearsTransparent = true
        hidesOnDeactivate = false
        becomesKeyOnlyIfNeeded = true
        backgroundColor = .clear
        isOpaque = false
        hasShadow = true
        standardWindowButton(.closeButton)?.isHidden = true
        standardWindowButton(.miniaturizeButton)?.isHidden = true
        standardWindowButton(.zoomButton)?.isHidden = true

        contentView = content
    }

    /// 和分析框一样：只看，不抢炉石的键盘焦点。
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    var contentHeight: CGFloat {
        content.fittingHeight(width: frame.width)
    }

    func apply(_ state: DecisionState) {
        let changed = state.headline != content.state.headline
            || state.reason != content.state.reason
            || state.costText != content.state.costText
            || state.urgency != content.state.urgency
            || state.hasContent != content.state.hasContent
        content.apply(state)
        if changed {
            let h = contentHeight
            if abs(h - frame.height) > 0.5 {
                var f = frame
                let top = frame.maxY
                f.size.height = h
                f.origin.y = top - h
                setFrame(f, display: true)
            }
        }
    }

    /// 贴到分析框上方，并夹在屏幕内。
    /// - Parameters:
    ///   - target: 炉石窗口矩形（左下原点）；nil 表示没找到
    ///   - screen: 目标屏幕可见区域
    ///   - top: 决策框的上边（通常取游戏窗口顶边，决策框是最上面那一层）
    func dock(target: CGRect?, screen: CGRect, top: CGFloat) {
        let h = frame.height
        let x = min(max(screen.minX, screen.maxX - frame.width - DockGeometry.gap),
                    screen.maxX - frame.width)
        // 顶到屏幕顶就贴着屏幕顶，绝不跑到屏幕外
        let maxY = screen.maxY - h
        setFrameOrigin(CGPoint(x: x, y: min(max(screen.minY, top - h), maxY)))
    }

    /// 炉石不在前台时一起淡出。
    func setGameActive(_ active: Bool) {
        if FloatingPanel.forceShow {
            alphaValue = 1.0
            ignoresMouseEvents = false
            return
        }
        alphaValue = active ? 1.0 : 0.0
        ignoresMouseEvents = !active
    }
}
