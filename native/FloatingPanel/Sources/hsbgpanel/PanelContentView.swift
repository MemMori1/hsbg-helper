// PanelContentView.swift
//
// 悬浮框的内容视图。
//
// ⚠️ 这里刻意**不使用 SwiftUI**：`swiftc` 直编 + 手搓 .app 外壳的前提下，
// AppKit 的 Auto Layout 更容易控制"内容驱动高度"（面板要按内容量自动伸缩）。
// 内容结构对齐 `src/hsbg/render.py` 的 `Renderer.panel()`，两边保持同一套信息层级。

import Cocoa

/// 一帧的展示数据。由 Python 侧（`hsbg panel`）以 JSON Lines 喂进来。
struct PanelContentState {
    var turn: Int = 0
    var phase: String = ""            // 招募 / 战斗 / 结束
    var side: String = ""             // 吸附在炉石哪一侧（right/left/overlay）
    var targetVisible: Bool = false   // 炉石窗口当前是否可见
    var source: String = ""           // 几何来源（AX / CGWindowList / 屏幕右缘）

    var headline: String = ""         // 一句话建议
    var gold: Int = 0
    var health: Int = 0
    var tavernTier: Int = 0
    var shop: [ShopRow] = []
    var spells: [SpellRow] = []
    var board: [MinionRow] = []
    var hand: [String] = []
    var opponents: [OpponentRow] = []
    var warnings: [WarningRow] = []
    var darkGift: DarkGiftRow?
    var pendingChoice: ChoiceRow?
    var trinkets: [TrinketRow] = []
    var levelAdvice: LevelRow?
    /// ⭐ 决策框的数据（独立小窗 `DecisionPanel` 读这个）
    var decision: DecisionState?
    var modelNote: String = ""
    var modelAnswers: [ModelAnswerRow] = []
    var stale: Bool = false           // 数据是否过期（游戏没在写日志）
    var error: String = ""

    struct ShopRow {
        var index: Int = 0            // 盘面上的显示序号（1..N）
        var slotEntityId: Int = 0     // 游戏内槽位 id（提问的 key）
        var name: String = ""
        var attack: Int = 0
        var health: Int = 0
        var techLevel: Int = 0
        var golden: Bool = false
        var score: Double = 0
        var action: String = ""       // 买 / 考虑 / 跳过
        var reasons: [String] = []
        var keywords: [String] = []       // 剧毒/圣盾/发动/进击…
        var modelBuyProbability: Double?  // JevK5 的 P(买)，nil = 模型没给出
    }

    struct MinionRow {
        var name: String = ""
        var attack: Int = 0
        var health: Int = 0
        var golden: Bool = false
        var taunt: Bool = false
        var divineShield: Bool = false
    }

    /// 酒馆法术：和随从分开——没有攻/血，买价也不是 3
    struct SpellRow {
        var index: Int = 0
        var name: String = ""
        var cost: Int = 0
        var techLevel: Int = 0
        var score: Double = 0
        var action: String = ""
        var reasons: [String] = []
        var text: String = ""
    }

    struct OpponentRow {
        var playerId: Int = 0
        var heroName: String = ""
        var health: Int = 0
        var tavernTier: Int = 0
        var isDead: Bool = false
    }

    struct WarningRow {
        var level: String = "info"    // danger / warn / info
        var text: String = ""
    }

    /// 「黑暗之赐」（BG36 机制）：能点了就提醒，已挂赐福的随从也列出来。
    struct DarkGiftRow {
        var available: Bool = false
        var granted: [String] = []
        var knownEffects: [String] = []
        var knownEffectCount: Int = 0
    }

    /// 待选（饰品四选一 / 黑赐三选一）。
    struct ChoiceRow {
        var kind: String = ""         // trinket / trinket_large / dark_gift
        var title: String = ""        // 小饰品四选一 …
        var sourceText: String = ""   // 触发源卡文（饰品效果等）
        var options: [ChoiceOptionRow] = []
    }

    struct ChoiceOptionRow {
        var index: Int = 0
        var name: String = ""
        var note: String = ""
        /// 模型给这一项的概率（0..1）；无模型时为 nil。
        /// 规则层对饰品/赐福刻意不打分，这是面板上唯一"选哪个"的量化依据。
        var probability: Double?
        /// 黑暗之赐的赐福（**另一张卡**，靠 `DARK_GIFT_ENTITY` 关联）。
        /// ⚠️ 黑赐三选一里选项是随从，但强弱主要由赐福给了什么词条决定
        /// （实测同一只随从可以配 圣盾+风怒 / +10血亡语 / 复生 三种）。
        var giftName: String = ""
        var giftNote: String = ""
    }

    /// 已装备的饰品（小/大）。
    struct TrinketRow {
        var size: String = ""         // small / large
        var name: String = ""
        var text: String = ""
    }

    struct LevelRow {
        var cost: Int = 0
        var affordable: Bool = false
        var currentTier: Int = 0
        var nextTier: Int = 0
        var action: String = ""       // 升 / 不升 / 观望
        var reasons: [String] = []
    }

    struct ModelAnswerRow {
        var question: String = ""
        var answer: String = ""
        var probability: Double = 0
        var seconds: Double = 0
    }
}

/// 面板内容视图：自绘，不用 Auto Layout 的子视图树。
final class PanelContentView: NSView {
    var onMoveBy: ((CGFloat, CGFloat) -> Void)?
    var onReset: (() -> Void)?
    var onRequestAccessibility: (() -> Void)?

    private var state = PanelContentState()
    private var statusMessage: String?
    private var statusUntil: Date?

    // 排版常量 —— 与 render.py 的 46 半角字宽对齐（中文按 2 计）。
    private let padding: CGFloat = 12
    private let lineHeight: CGFloat = 17
    private let sectionGap: CGFloat = 8
    private let headingHeight: CGFloat = 19

    private let monoFont = NSFont.monospacedSystemFont(ofSize: 11.5, weight: .regular)
    private let headingFont = NSFont.systemFont(ofSize: 11.5, weight: .semibold)

    override var isFlipped: Bool { true }   // 从上往下排，跟文本一致的直觉

    func apply(_ newState: PanelContentState) {
        state = newState
        needsDisplay = true
    }

    func setSide(_ side: DockGeometry.Side, targetVisible: Bool) {
        state.side = side.rawValue
        state.targetVisible = targetVisible
    }

    func flashStatus(_ message: String) {
        statusMessage = message
        statusUntil = Date().addingTimeInterval(1.6)
        needsDisplay = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.7) { [weak self] in
            self?.statusMessage = nil
            self?.needsDisplay = true
        }
    }

    // MARK: - 高度

    /// 量一次内容需要多高 —— 面板据此伸缩。
    func fittingHeight(width: CGFloat) -> CGFloat {
        var h = padding
        h += headingHeight                                   // 行情
        h += lineHeight * CGFloat(shopHeaderLines)
        if !state.shop.isEmpty { h += lineHeight * CGFloat(state.shop.count * 2) }
        if !state.board.isEmpty {
            h += sectionGap + headingHeight + lineHeight * CGFloat(boardLines)
        }
        if !state.hand.isEmpty { h += sectionGap + lineHeight }
        if !state.opponents.isEmpty { h += sectionGap + lineHeight }
        if let _ = state.levelAdvice { h += sectionGap + lineHeight }
        if state.darkGift != nil { h += sectionGap + lineHeight * 2 }
        // 待选：标题 + 每项一行（项多时按两行/项估，卡文提示会换行）
        if let c = state.pendingChoice {
            h += sectionGap + headingHeight
            h += lineHeight * CGFloat(max(1, c.options.count))
            // 每个选项最多再加一行 note + 一行赐福（黑赐才有赐福）
            for o in c.options {
                if !o.note.isEmpty { h += lineHeight }
                if !o.giftNote.isEmpty { h += lineHeight }
            }
        }
        if !state.trinkets.isEmpty {
            h += sectionGap + lineHeight * CGFloat(state.trinkets.count)
        }
        if !state.spells.isEmpty {
            h += sectionGap + headingHeight + lineHeight * CGFloat(state.spells.count) * 2
        }
        if !state.warnings.isEmpty { h += sectionGap + lineHeight * CGFloat(state.warnings.count) }
        if !state.modelAnswers.isEmpty {
            h += sectionGap + headingHeight + lineHeight * CGFloat(1 + state.modelAnswers.count)
        }
        if !state.modelNote.isEmpty { h += sectionGap + headingHeight + lineHeight }
        if !statusMessage.isNilOrEmpty { h += lineHeight }
        h += padding
        _ = width
        return h
    }

    private var shopHeaderLines: Int { state.shop.isEmpty ? 1 : 1 }

    /// 自家场面按每行 2 只排（跟 render.py 一样省高度）。
    private var boardLines: Int { max(1, Int(ceil(Double(state.board.count) / 2.0))) }

    // MARK: - 绘制

    override func draw(_ dirtyRect: NSRect) {
        NSColor.clear.set()
        dirtyRect.fill()

        let bg = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5),
                              xRadius: 10, yRadius: 10)
        NSColor(calibratedWhite: 0.08, alpha: 0.93).setFill()
        bg.fill()
        NSColor(calibratedWhite: 1.0, alpha: 0.14).setStroke()
        bg.lineWidth = 1
        bg.stroke()

        var y = padding
        y = drawHeadline(at: y)
        y = drawShop(from: y)
        y = drawSpells(from: y)
        y = drawBoard(from: y)
        y = drawMisc(from: y)
        y = drawModel(from: y)
        _ = drawStatus(from: y)
    }

    private func draw(_ text: String, at x: CGFloat, _ y: CGFloat,
                      color: NSColor, font: NSFont? = nil,
                      reserveRight: CGFloat = 0) {
        let f = font ?? monoFont
        // ⚠️ 必须自己截断：AppKit 的 `draw(at:)` 不会裁剪，超出面板宽度的字
        // 会直接画到面板外面去（实测"升本"和"对手"两行都是被切一半的）。
        let avail = bounds.width - x - padding - reserveRight
        let clipped = clipToWidth(text, maxWidth: avail, font: f)
        let attrs: [NSAttributedString.Key: Any] = [
            .font: f,
            .foregroundColor: color,
        ]
        clipped.draw(at: NSPoint(x: x, y: y), withAttributes: attrs)
    }

    /// 按可用宽度截断，超长则用 `…` 收尾。
    ///
    /// 逐字符量宽而不是按字数估：等宽字体里 `★`、中文、`P(买)=0.44` 三种宽度都不同，
    /// 估算一定会在某个组合上溢出。
    private func clipToWidth(_ text: String, maxWidth: CGFloat, font: NSFont) -> String {
        guard maxWidth > 8 else { return "" }
        let full = (text as NSString).size(withAttributes: [.font: font]).width
        if full <= maxWidth { return text }
        let ellipsis = "…"
        let ellipsisWidth = (ellipsis as NSString).size(withAttributes: [.font: font]).width
        var out = ""
        var width: CGFloat = 0
        for ch in text {
            let w = (String(ch) as NSString).size(withAttributes: [.font: font]).width
            if width + w + ellipsisWidth > maxWidth { break }
            out.append(ch)
            width += w
        }
        return out + ellipsis
    }

    private func line(_ text: String, at x: CGFloat, _ y: CGFloat, color: NSColor,
                      font: NSFont? = nil) -> CGFloat {
        draw(text, at: x, y, color: color, font: font)
        return y + lineHeight
    }

    private func drawHeadline(at y0: CGFloat) -> CGFloat {
        var y = y0
        let hasFrame = !state.headline.isEmpty
        let text = hasFrame ? state.headline : "等待炉石开局…"
        let color = NSColor(calibratedRed: 1.0, green: 0.84, blue: 0.4, alpha: 1)
        draw(text, at: padding, y, color: color, font: headingFont)
        y += headingHeight

        // ⚠️ 有帧时不要再拼一遍元信息：`headline` 本身就是
        // "回合 3 招募 · 0 金 · 30 血 · 2 本"，再画一行几乎一样的会显得很啰嗦
        // （实测截图上就是两行重复）。只在还没有数据时用它撑住面板高度。
        let bits = [
            state.phase.isEmpty ? nil : state.phase,
            "回合 \(state.turn)",
            "\(state.gold) 金",
            state.health > 0 ? "\(state.health) 血" : nil,
            state.tavernTier > 0 ? "\(state.tavernTier) 本" : nil,
        ].compactMap { $0 }
        var metaLine = hasFrame ? "" : bits.joined(separator: " · ")
        if !state.targetVisible {
            metaLine += (metaLine.isEmpty ? "" : "   ") + "[炉石未在前台]"
        }
        if state.stale {
            metaLine += (metaLine.isEmpty ? "" : "   ") + "[数据未更新]"
        }
        if !metaLine.isEmpty {
            y = line(metaLine, at: padding, y, color: NSColor(calibratedWhite: 0.72, alpha: 1))
        }
        return y + 2
    }

    private func drawShop(from y0: CGFloat) -> CGFloat {
        var y = y0
        let header = state.shop.isEmpty
            ? "商店（空）"
            : "商店（\(state.shop.count) 张）"
        y = line(header, at: padding, y, color: NSColor(calibratedWhite: 0.55, alpha: 1))

        for row in state.shop {
            var name = row.name
            if row.golden { name = "★" + name }
            var text = "\(row.index). \(name)  \(row.attack)/\(row.health)"
            if row.techLevel > 0 { text += "  \(row.techLevel)星" }
            // ⚠️ 这里原本是 `if row.score > 0`，负分被整段吞掉 —— 实测商店里
            // 评分 -1.4 / -1.8 的行会渲染成"没有分数"，看着像规则引擎没给出意见，
            // 而实际上它明确判定为"不值"。负分恰恰是最该让用户看到的信号。
            // 只把 score == 0（卡表查不到、引擎没打分）当作"无意见"隐去。
            if row.score != 0 { text += String(format: "  %.1f分", row.score) }

            var prob: String?
            if let p = row.modelBuyProbability {
                prob = String(format: "模型 P(买)=%.2f", p)
            }
            let probWidth = prob.map {
                ($0 as NSString).size(withAttributes: [.font: monoFont]).width
            } ?? 0
            draw(text, at: padding, y,
                 color: row.action == "买"
                     ? NSColor(calibratedRed: 0.5, green: 0.95, blue: 0.6, alpha: 1)
                     : NSColor(calibratedWhite: 0.88, alpha: 1),
                 // 有模型概率时给右侧留出位置，否则长卡名会把概率挤到面板外。
                 reserveRight: prob != nil ? probWidth + 8 : 0)
            if let prob = prob {
                let w = (text as NSString).size(withAttributes: [.font: monoFont]).width
                let x = min(padding + w + 8, bounds.width - padding - probWidth)
                draw(prob, at: max(padding, x), y,
                     color: NSColor(calibratedRed: 0.6, green: 0.75, blue: 1.0, alpha: 1))
            }
            y += lineHeight

            // 关键词放在最前：它是"这卡凭什么值这个分"的最直接解释。
            // ⚠️ 必须从 `reasons` 里剔掉同一条：规则层在关键词加分时也会 append
            // 一句 `关键词：…`，两边都留着就会渲染成"关键词: 发动；关键词: 发动"。
            let kw = row.keywords.isEmpty ? "" : "关键词：" + row.keywords.joined(separator: "/")
            let rest = row.reasons.filter { !$0.hasPrefix("关键词：") }
            let detail = ([row.action, kw] + rest).filter { !$0.isEmpty }.joined(separator: "；")
            y = line("    " + detail, at: padding, y,
                     color: NSColor(calibratedWhite: 0.58, alpha: 1))
        }
        return y
    }

    /// 酒馆法术：单独一段。没有攻/血，所以展示「N金 + 星级 + 分数」和卡文。
    private func drawSpells(from y0: CGFloat) -> CGFloat {
        guard !state.spells.isEmpty else { return y0 }
        var y = y0 + sectionGap
        y = line("法术（\(state.spells.count) 张）", at: padding, y,
                 color: NSColor(calibratedWhite: 0.55, alpha: 1))
        for row in state.spells {
            var text = "\(row.index). \(row.name)  \(row.cost)金"
            if row.techLevel > 0 { text += "  \(row.techLevel)星" }
            if row.score != 0 { text += String(format: "  %.1f分", row.score) }
            draw(text, at: padding, y,
                 color: row.action == "买"
                     ? NSColor(calibratedRed: 0.5, green: 0.95, blue: 0.6, alpha: 1)
                     : NSColor(calibratedWhite: 0.88, alpha: 1))
            y += lineHeight
            let kw = row.reasons.filter { !$0.hasPrefix("关键词：") }
            let detail = ([row.action] + kw).filter { !$0.isEmpty }.joined(separator: "；")
            if !detail.isEmpty {
                y = line("    " + detail, at: padding, y,
                         color: NSColor(calibratedWhite: 0.58, alpha: 1))
            }
            if !row.text.isEmpty {
                y = line("    " + row.text, at: padding, y,
                         color: NSColor(calibratedWhite: 0.50, alpha: 1))
            }
        }
        return y
    }

    private func drawBoard(from y0: CGFloat) -> CGFloat {
        guard !state.board.isEmpty else { return y0 }
        var y = y0 + sectionGap
        y = line("自家场面（\(state.board.count)）", at: padding, y,
                 color: NSColor(calibratedWhite: 0.55, alpha: 1))

        var rowText = ""
        var rowCount = 0
        for m in state.board {
            var s = m.name
            if m.golden { s = "★" + s }
            if m.taunt { s += "†" }
            if m.divineShield { s += "◇" }
            s += " \(m.attack)/\(m.health)"
            rowText += (rowText.isEmpty ? "" : "   ") + s
            rowCount += 1
            if rowCount == 2 {
                y = line(rowText, at: padding, y, color: NSColor(calibratedWhite: 0.88, alpha: 1))
                rowText = ""
                rowCount = 0
            }
        }
        if !rowText.isEmpty {
            y = line(rowText, at: padding, y, color: NSColor(calibratedWhite: 0.88, alpha: 1))
        }
        return y
    }

    private func drawMisc(from y0: CGFloat) -> CGFloat {
        var y = y0
        // 待选（饰品四选一 / 黑赐三选一）排在最前：不做决定会卡住回合。
        if let c = state.pendingChoice {
            y += sectionGap
            let head = c.sourceText.isEmpty ? c.title + "（还没选）"
                                             : c.title + "（还没选）  " + c.sourceText
            y = line(head, at: padding, y,
                     color: NSColor(calibratedRed: 1.0, green: 0.83, blue: 0.4, alpha: 1),
                     font: headingFont)
            for o in c.options {
                let label = "\(o.index + 1). \(o.name)"
                if let p = o.probability {
                    // 概率排在名字右侧；`draw` 自带截断，长卡名会在概率前收尾。
                    let pText = String(format: "P=%.2f", p)
                    let pw = (pText as NSString).size(withAttributes: [
                        .font: monoFont]).width
                    let rowY = y
                    draw(label, at: padding, rowY,
                         color: NSColor(calibratedWhite: 0.92, alpha: 1),
                         font: nil, reserveRight: pw + 8)
                    draw(pText, at: bounds.width - padding - pw, rowY,
                         color: NSColor(calibratedRed: 0.6, green: 0.9, blue: 0.6,
                                        alpha: 1),
                         font: monoFont)
                    y += lineHeight
                } else {
                    y = line(label, at: padding, y,
                             color: NSColor(calibratedWhite: 0.92, alpha: 1))
                }
                if !o.note.isEmpty {
                    y = line("    " + o.note, at: padding, y,
                             color: NSColor(calibratedWhite: 0.55, alpha: 1))
                }
                // 黑暗之赐的赐福单独一行高亮（金色）：同一只随从可以配不同
                // 赐福，赐福给的词条才是"这个选项强不强"的主要依据。
                if !o.giftNote.isEmpty {
                    y = line("    " + o.giftNote, at: padding, y,
                             color: NSColor(calibratedRed: 0.98, green: 0.8,
                                            blue: 0.45, alpha: 1))
                }
            }
        }
        // 已装备的饰品
        for tr in state.trinkets {
            y += sectionGap
            let size = tr.size == "large" ? "大饰品" : "小饰品"
            let body = tr.text.isEmpty ? tr.name : "\(tr.name)  \(tr.text)"
            y = line("\(size)  \(body)", at: padding, y,
                     color: NSColor(calibratedRed: 0.7, green: 0.85, blue: 1.0, alpha: 1))
        }
        if !state.hand.isEmpty {
            y += sectionGap
            y = line("手牌（\(state.hand.count)）：" + state.hand.joined(separator: "、"),
                     at: padding, y, color: NSColor(calibratedWhite: 0.75, alpha: 1))
        }
        if !state.opponents.isEmpty {
            y += sectionGap
            let parts = state.opponents.map { o -> String in
                let name = o.heroName.isEmpty ? "P\(o.playerId)" : o.heroName
                return "\(name) \(o.health)"
            }
            y = line("对手：" + parts.joined(separator: "  "), at: padding, y,
                     color: NSColor(calibratedWhite: 0.72, alpha: 1))
        }
        if let g = state.darkGift {
            y += sectionGap
            let head = g.available ? "黑暗发现（可点！）" : "黑暗之赐"
            let body = g.granted.isEmpty ? "尚未挂上" : g.granted.joined(separator: "、")
            y = line("\(head)：\(body)", at: padding, y,
                     color: g.available
                         ? NSColor(calibratedRed: 0.5, green: 0.95, blue: 0.6, alpha: 1)
                         : NSColor(calibratedWhite: 0.8, alpha: 1))
            if !g.knownEffects.isEmpty {
                let more = g.knownEffectCount > g.knownEffects.count
                    ? "（共 \(g.knownEffectCount) 种）" : ""
                y = line("    本局见过\(more)：" + g.knownEffects.joined(separator: "、"),
                         at: padding, y, color: NSColor(calibratedWhite: 0.55, alpha: 1))
            } else {
                y += lineHeight
            }
        }
        if let lv = state.levelAdvice {
            y += sectionGap
            let cost = lv.cost > 0 ? "\(lv.cost) 金" : "费用未知"
            let text = "升本 \(lv.currentTier)本→\(lv.nextTier)本  费用 \(cost)  \(lv.action)"
            let reasons = lv.reasons.isEmpty ? "" : "  " + lv.reasons.joined(separator: "；")
            y = line(text + reasons, at: padding, y,
                     color: lv.affordable
                         ? NSColor(calibratedRed: 0.5, green: 0.95, blue: 0.6, alpha: 1)
                         : NSColor(calibratedWhite: 0.6, alpha: 1))
        }
        for (i, w) in state.warnings.enumerated() {
            if i == 0 { y += sectionGap }
            let color: NSColor
            switch w.level {
            case "danger": color = NSColor(calibratedRed: 1.0, green: 0.42, blue: 0.42, alpha: 1)
            case "warn":   color = NSColor(calibratedRed: 1.0, green: 0.78, blue: 0.35, alpha: 1)
            default:       color = NSColor(calibratedWhite: 0.72, alpha: 1)
            }
            y = line("⚠ " + w.text, at: padding, y, color: color)
        }
        return y
    }

    private func drawModel(from y0: CGFloat) -> CGFloat {
        var y = y0
        if !state.modelNote.isEmpty {
            y += sectionGap
            y = line(state.modelNote, at: padding, y,
                     color: NSColor(calibratedWhite: 0.6, alpha: 1))
        }
        guard !state.modelAnswers.isEmpty else { return y }
        y += sectionGap
        y = line("模型意见", at: padding, y, color: NSColor(calibratedWhite: 0.55, alpha: 1))
        for a in state.modelAnswers {
            let text = String(format: "%@  %@  P=%.2f", a.question, a.answer, a.probability)
            y = line("  " + text, at: padding, y,
                     color: NSColor(calibratedRed: 0.62, green: 0.76, blue: 1.0, alpha: 1))
        }
        return y
    }

    private func drawStatus(from y0: CGFloat) -> CGFloat {
        guard let msg = statusMessage, !msg.isEmpty else { return y0 }
        return line(msg, at: padding, y0 + sectionGap,
                    color: NSColor(calibratedRed: 0.5, green: 0.95, blue: 0.6, alpha: 1))
    }

    // MARK: - 拖动 / 双击贴回

    override func mouseDragged(with event: NSEvent) {
        onMoveBy?(event.deltaX, -event.deltaY)   // isFlipped，y 轴相反
    }

    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 {
            onReset?()
        }
    }

    override func rightMouseDown(with event: NSEvent) {
        let menu = NSMenu()
        let reset = NSMenuItem(title: "贴回炉石右侧", action: #selector(handleReset), keyEquivalent: "")
        reset.target = self
        menu.addItem(reset)
        if !AXIsProcessTrusted() {
            let improve = NSMenuItem(title: "启用辅助功能以提高跟随精度…",
                                     action: #selector(handleAccessibility), keyEquivalent: "")
            improve.target = self
            menu.addItem(improve)
        }
        NSMenu.popUpContextMenu(menu, with: event, for: self)
    }

    @objc private func handleReset() { onReset?() }
    @objc private func handleAccessibility() { onRequestAccessibility?() }
}

private extension Optional where Wrapped == String {
    var isNilOrEmpty: Bool {
        switch self {
        case .none: return true
        case .some(let s): return s.isEmpty
        }
    }
}
