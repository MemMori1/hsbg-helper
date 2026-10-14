// DataFeed.swift
//
// 从 `hsbg panel --json` 读一帧一行的数据，喂给悬浮框。
//
// 设计取舍：用**子进程 + stdout 换行分隔 JSON**，而不是 HTTP 或 socket。
//   - 零端口占用、零鉴权
//   - Python 侧被杀掉/崩溃时 Swift 侧能立刻感知（EOF），自动重连
//   - 调试极简单：`./hsbg panel --json | head -1` 就是全部协议

import Cocoa

protocol DataFeedDelegate: AnyObject {
    func dataFeed(_ feed: DataFeed, didReceive state: PanelContentState)
    func dataFeed(_ feed: DataFeed, didFail message: String)
}

final class DataFeed {
    weak var delegate: DataFeedDelegate?

    /// 项目根目录（包含 `hsbg` 可执行包装器的那个目录）。
    /// 可以用环境变量 `HSBG_HOME` 覆盖，方便把 .app 拷到别处。
    let home: URL
    /// 额外传给 `hsbg panel` 的参数。
    var extraArguments: [String] = []

    private var process: Process?
    private var buffer = Data()
    private var readSource: DispatchSourceRead?
    private var restartDelay: TimeInterval = 1.0
    private var stopped = false

    init(home: URL? = nil) {
        if let home = home {
            self.home = home
        } else if let env = ProcessInfo.processInfo.environment["HSBG_HOME"] {
            self.home = URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        } else {
            // 默认：.app 在 native/FloatingPanel/build/ 下，项目根往上退 5 层；
            // 找不到就退回当前工作目录。
            let guess = URL(fileURLWithPath: CommandLine.arguments[0])
                .resolvingSymlinksInPath()
                .deletingLastPathComponent()
            self.home = DataFeed.discoverHome(startingAt: guess)
        }
    }

    static func discoverHome(startingAt url: URL) -> URL {
        var dir = url
        for _ in 0..<8 {
            let candidate = dir.appendingPathComponent("hsbg")
            if FileManager.default.isExecutableFile(atPath: candidate.path) {
                return dir
            }
            let parent = dir.deletingLastPathComponent()
            if parent.path == dir.path { break }
            dir = parent
        }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    }

    func start() {
        stopped = false
        launch()
    }

    /// 极简诊断日志（`/tmp/hsbgpanel.log`）。
    ///
    /// 面板是 `LSUIElement` 的后台 app，`print` 的去向不可靠；子进程拉不起来时
    /// 界面上只会显示"等待炉石开局"，看不出原因。写文件是最省事的排查手段。
    static func trace(_ message: String) {
        let path = "/tmp/hsbgpanel.log"
        let line = String(format: "%.3f %@\n", Date().timeIntervalSince1970, message)
        guard let data = line.data(using: .utf8) else { return }
        if let h = FileHandle(forWritingAtPath: path) {
            h.seekToEndOfFile()
            h.write(data)
            try? h.close()
        } else {
            try? data.write(to: URL(fileURLWithPath: path))
        }
    }

    func stop() {
        stopped = true
        readSource?.cancel()
        readSource = nil
        process?.terminationHandler = nil
        process?.terminate()
        process = nil
    }

    private func launch() {
        let hsbg = home.appendingPathComponent("hsbg")
        guard FileManager.default.isExecutableFile(atPath: hsbg.path) else {
            DataFeed.trace("找不到可执行文件 \(hsbg.path)")
            delegate?.dataFeed(self, didFail: "找不到 \(hsbg.path)")
            return
        }
        DataFeed.trace("launch \(hsbg.path) panel \(extraArguments.joined(separator: " "))")

        let proc = Process()
        proc.executableURL = hsbg
        // ⚠️ 不要传 `--json`：`panel` 本身**永远输出 JSON Lines**，没有这个开关。
        // 早先这里多传了一个 `--json`，argparse 直接 `unrecognized arguments`
        // 退出码 2，面板上一直显示"等待炉石开局"（踩过的坑）。
        proc.arguments = ["panel"] + extraArguments
        proc.currentDirectoryURL = home

        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"   // 不缓冲，否则一帧都收不到
        env["PYTHONIOENCODING"] = "utf-8"
        proc.environment = env

        let pipe = Pipe()
        proc.standardOutput = pipe
        let errPipe = Pipe()
        proc.standardError = errPipe    // 错误走 stderr，别混进 JSON 流
        // ⚠️ stderr 必须有人读，否则子进程写满管道会阻塞（实测表现为"一帧都收不到"）。
        errPipe.fileHandleForReading.readabilityHandler = { h in
            let d = h.availableData
            guard !d.isEmpty, let s = String(data: d, encoding: .utf8) else { return }
            DataFeed.trace("stderr: " + s.trimmingCharacters(in: .whitespacesAndNewlines))
        }

        proc.terminationHandler = { [weak self] p in
            DataFeed.trace("子进程退出 status=\(p.terminationStatus)")
            guard let self = self, !self.stopped else { return }
            DispatchQueue.main.asyncAfter(deadline: .now() + self.restartDelay) {
                guard !self.stopped else { return }
                self.restartDelay = min(self.restartDelay * 2, 15)
                self.launch()
            }
        }

        do {
            try proc.run()
        } catch {
            DataFeed.trace("启动失败：\(error.localizedDescription)")
            delegate?.dataFeed(self, didFail: "启动失败：\(error.localizedDescription)")
            return
        }
        DataFeed.trace("已启动 pid=\(proc.processIdentifier)")
        process = proc
        restartDelay = 1.0

        let fd = pipe.fileHandleForReading.fileDescriptor
        let source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: .main)
        source.setEventHandler { [weak self] in
            guard let self = self else { return }
            let data = pipe.fileHandleForReading.availableData
            if data.isEmpty { return }   // EOF 由 terminationHandler 处理
            self.buffer.append(data)
            self.drainLines()
        }
        source.setCancelHandler { try? pipe.fileHandleForReading.close() }
        source.resume()
        readSource = source
    }

    private func drainLines() {
        let newline = UInt8(ascii: "\n")
        while let idx = buffer.firstIndex(of: newline) {
            let lineData = buffer.subdata(in: buffer.startIndex..<idx)
            buffer.removeSubrange(buffer.startIndex...idx)
            guard !lineData.isEmpty else { continue }
            handle(line: lineData)
        }
        // 防御：单帧不可能超过 1 MB，超了说明流坏了。
        if buffer.count > 1 << 20 { buffer.removeAll() }
    }

    private func handle(line: Data) {
        guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else {
            return
        }
        delegate?.dataFeed(self, didReceive: PanelContentState(json: obj))
    }
}

// MARK: - JSON → PanelContentState

extension PanelContentState {
    init(json: [String: Any]) {
        self.init()
        turn = json["turn"] as? Int ?? 0
        phase = json["phase"] as? String ?? ""
        gold = json["gold"] as? Int ?? 0
        health = json["health"] as? Int ?? 0
        tavernTier = json["tavern_tier"] as? Int ?? 0
        headline = json["headline"] as? String ?? ""
        stale = json["stale"] as? Bool ?? false
        error = json["error"] as? String ?? ""
        modelNote = json["model_note"] as? String ?? ""

        for raw in (json["shop"] as? [[String: Any]]) ?? [] {
            var row = ShopRow()
            row.index = raw["display_index"] as? Int ?? (raw["index"] as? Int ?? 0)
            row.slotEntityId = raw["slot_entity_id"] as? Int ?? 0
            row.name = raw["name"] as? String ?? ""
            row.attack = raw["attack"] as? Int ?? 0
            row.health = raw["health"] as? Int ?? 0
            row.techLevel = raw["tech_level"] as? Int ?? 0
            row.golden = raw["golden"] as? Bool ?? false
            row.score = raw["score"] as? Double ?? 0
            row.action = raw["action"] as? String ?? ""
            row.reasons = (raw["reasons"] as? [String]) ?? []
            row.keywords = (raw["keywords"] as? [String]) ?? []
            row.modelBuyProbability = raw["model_buy_probability"] as? Double
            shop.append(row)
        }

        for raw in (json["board"] as? [[String: Any]]) ?? [] {
            var row = MinionRow()
            row.name = raw["name"] as? String ?? ""
            row.attack = raw["attack"] as? Int ?? 0
            row.health = raw["health"] as? Int ?? 0
            row.golden = raw["golden"] as? Bool ?? false
            row.taunt = raw["taunt"] as? Bool ?? false
            row.divineShield = raw["divine_shield"] as? Bool ?? false
            board.append(row)
        }

        hand = (json["hand"] as? [String]) ?? []

        for raw in (json["opponents"] as? [[String: Any]]) ?? [] {
            var row = OpponentRow()
            row.playerId = raw["player_id"] as? Int ?? 0
            row.heroName = raw["hero_name"] as? String ?? ""
            row.health = raw["health"] as? Int ?? 0
            row.tavernTier = raw["tavern_tier"] as? Int ?? 0
            row.isDead = raw["is_dead"] as? Bool ?? false
            opponents.append(row)
        }

        for raw in (json["warnings"] as? [[String: Any]]) ?? [] {
            var row = WarningRow()
            row.level = raw["level"] as? String ?? "info"
            row.text = raw["text"] as? String ?? ""
            warnings.append(row)
        }

        if let raw = json["dark_gift"] as? [String: Any] {
            var g = DarkGiftRow()
            g.available = raw["available"] as? Bool ?? false
            g.granted = (raw["granted"] as? [String]) ?? []
            g.knownEffects = (raw["known_effects"] as? [String]) ?? []
            g.knownEffectCount = raw["known_effect_count"] as? Int ?? g.knownEffects.count
            darkGift = g
        }

        if let raw = json["pending_choice"] as? [String: Any] {
            var c = ChoiceRow()
            c.kind = raw["kind"] as? String ?? ""
            c.title = raw["title"] as? String ?? "待选"
            c.sourceText = raw["source_text"] as? String ?? ""
            for o in (raw["options"] as? [[String: Any]]) ?? [] {
                var row = ChoiceOptionRow()
                row.index = o["index"] as? Int ?? 0
                row.name = o["name"] as? String ?? ""
                row.note = o["note"] as? String ?? ""
                row.probability = o["probability"] as? Double
                // 黑暗之赐的赐福（另一张卡）：黑赐三选一的强弱主要看它
                row.giftName = o["gift_name"] as? String ?? ""
                row.giftNote = o["gift_note"] as? String ?? ""
                c.options.append(row)
            }
            if !c.options.isEmpty { pendingChoice = c }
        }

        for raw in (json["trinkets"] as? [[String: Any]]) ?? [] {
            var row = TrinketRow()
            row.size = raw["size"] as? String ?? ""
            row.name = raw["name"] as? String ?? ""
            row.text = raw["text"] as? String ?? ""
            trinkets.append(row)
        }

        if let raw = json["level_advice"] as? [String: Any] {
            var row = LevelRow()
            row.cost = raw["cost"] as? Int ?? 0
            row.affordable = raw["affordable"] as? Bool ?? false
            row.currentTier = raw["current_tier"] as? Int ?? 0
            row.nextTier = raw["next_tier"] as? Int ?? 0
            row.action = raw["action"] as? String ?? ""
            row.reasons = (raw["reasons"] as? [String]) ?? []
            levelAdvice = row
        }

        if let raw = json["decision"] as? [String: Any] {
            decision = DecisionState(json: raw)
        }

        for raw in (json["model_answers"] as? [[String: Any]]) ?? [] {
            var row = ModelAnswerRow()
            row.question = raw["question"] as? String ?? ""
            row.answer = raw["answer"] as? String ?? ""
            row.probability = raw["probability"] as? Double ?? 0
            row.seconds = raw["seconds"] as? Double ?? 0
            modelAnswers.append(row)
        }
    }
}
