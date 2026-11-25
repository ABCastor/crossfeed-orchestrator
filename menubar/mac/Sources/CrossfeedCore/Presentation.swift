// Crossfeed menu bar: turns a Glance into what the menu draws. Pure functions of (Glance, now):
// every string, bar position and colour decision is made here so the tests can read it.
//
// Structure follows CodexBar's menu (MIT, Copyright (c) 2026 Peter Steinberger; see NOTICE):
// provider switcher tabs, a card with header and labelled usage bars, a pace marker per bar.
import Foundation

public enum PaceKind: Equatable {
    case surplus, onPace, deficit, unknown
}

/// One usage bar. `expected` is where an even burn would be by now; the marker is drawn there.
public struct BarModel: Equatable {
    public var used: Double          // 0...100
    public var expected: Double?     // 0...100, nil without a window length
    public var pace: PaceKind
    /// Terracotta fill: in deficit, or nearly full. Never color alone: the marker position and words agree.
    public var hot: Bool

    public init(used: Double, expected: Double?, pace: PaceKind, hot: Bool) {
        self.used = used; self.expected = expected; self.pace = pace; self.hot = hot
    }
}

public struct RowModel: Equatable, Identifiable {
    public var id: String
    public var title: String
    public var usedText: String       // "2% used", "Not read"
    public var resetText: String?     // "Resets in 3d 20h"
    public var paceText: String?      // "Pace: 46% in deficit · runs out in 9h 39m"
    public var bar: BarModel?
}

public struct SectionModel: Equatable, Identifiable {
    public var id: String
    public var title: String?         // pool name, shown when a tab holds several pools
    public var badge: String?         // "Conserve", "Abundant"
    public var note: String?          // why a pool is not read
    public var rows: [RowModel]
    public var logo: ProviderLogo?
    public var level: SpendLevel?
    public var attribution: PolicyAttribution?
    public var models: [String]
}

public enum ProviderLogo: String, CaseIterable {
    case claude = "claude-color", openai, copilot = "githubcopilot", gemini = "gemini-color"
    case antigravity = "antigravity-color", opencode, openrouter

    static func forPool(_ id: String) -> Self? {
        if id == "claude" { return .claude }
        if id.hasPrefix("codex") || id.hasPrefix("chatgpt") { return .openai }
        if id.hasPrefix("github-copilot") { return .copilot }
        if id.hasPrefix("antigravity") { return .antigravity }
        if id.hasPrefix("gemini") { return .gemini }
        if id.hasPrefix("opencode") { return .opencode }
        if id.hasPrefix("openrouter") { return .openrouter }
        return nil
    }
}

public struct TabModel: Equatable, Identifiable {
    public var id: String
    public var title: String
    public var logo: ProviderLogo?    // nil for a group without one provider (Other)
    public var used: Double?          // the tab's tightest window, for the mini bar under it
    public var pace: PaceKind
    public var hot: Bool

    public init(id: String, title: String, logo: ProviderLogo?, used: Double?, pace: PaceKind, hot: Bool) {
        self.id = id; self.title = title; self.logo = logo; self.used = used; self.pace = pace; self.hot = hot
    }
}

public struct CardModel: Equatable {
    public var title: String
    public var subtitle: String       // "Updated 2 min ago"
    public var badge: String?
    public var sections: [SectionModel]
    public var isAntigravity: Bool

    public var heading: String { isAntigravity ? "Google AI Pro" : title }

    /// An allowance belongs to a routing pool, not to a reset timer. Gemini keeps one policy
    /// underneath both windows; the other two allowances have their own policy and model list.
    public var allowanceSections: [SectionModel] {
        guard isAntigravity else { return sections }
        let order = ["antigravity-gemini", "antigravity-3p", "antigravity-argon"]
        let titles = ["antigravity-gemini": "Gemini", "antigravity-3p": "Claude/GPT", "antigravity-argon": "Argon"]
        return sections.sorted { (order.firstIndex(of: $0.id) ?? order.count) < (order.firstIndex(of: $1.id) ?? order.count) }
            .map { section in
                guard let family = titles[section.id] else { return section }
                var result = section
                result.title = nil
                let sorted = section.rows.sorted { lhs, rhs in
                    lhs.title.hasPrefix("Weekly") && !rhs.title.hasPrefix("Weekly")
                }
                result.rows = sorted.map { row in
                    var named = row
                    named.title = family + " " + row.title.lowercased()
                    return named
                }
                let expected = section.id == "antigravity-gemini" ? ["weekly", "5-hour"] : ["weekly"]
                for (index, title) in expected.enumerated() where !result.rows.contains(where: { $0.title.hasPrefix(family + " " + title) }) {
                    result.rows.insert(RowModel(id: section.id + ":unread-" + title, title: family + " " + title,
                                                usedText: "Not read", resetText: nil, paceText: nil, bar: nil),
                                       at: min(index, result.rows.count))
                }
                return result
            }
    }
}

/// CodexBar's "Extra usage" card, for the ledger: how much of the month's subscriptions is the operator's.
public struct ShareModel: Equatable {
    public var title: String          // "Your share"
    public var fraction: Double       // 0...1
    public var left: String           // "EUR 10.00 of EUR 20.00"
    public var right: String          // "50% yours"
}

/// CodexBar's "Cost" card, for the ledger: what the month's subscriptions come to.
public struct SpendModel: Equatable {
    public var title: String          // "Subscriptions"
    public var lines: [String]        // "This month: EUR 20.00"
    public var note: String?          // "2 plans have no price yet"
    public var share: ShareModel?
}

/// The two tanks of the menu bar mark plus the number beside them.
public struct BarSummary: Equatable {
    public var text: String           // "88%", or "" when nothing is read
    public var left: Double?          // used %, most pressed pool
    public var right: Double?         // used %, next pool
    public var deficit: Bool
    public var tooltip: String

    public init(text: String, left: Double?, right: Double?, deficit: Bool, tooltip: String) {
        self.text = text; self.left = left; self.right = right; self.deficit = deficit; self.tooltip = tooltip
    }
}

public struct Presenter {
    public let glance: Glance
    public let loadedAt: Date
    public let now: Date

    public init(glance: Glance, loadedAt: Date, now: Date) {
        self.glance = glance
        self.loadedAt = loadedAt
        self.now = now
    }

    // MARK: Tabs

    static let order = ["claude", "codex", "copilot", "agy", "go"]
    static let titles = ["claude": "Claude", "codex": "Codex", "copilot": "Copilot", "agy": "AGY",
                         "go": "OpenCode", "other": "Other"]
    static let cardTitles = ["agy": "Antigravity"]

    static func groupID(_ pool: Pool) -> String {
        let id = pool.pool
        if ["antigravity-gemini", "antigravity-3p", "antigravity-argon"].contains(id) { return "agy" }
        if !pool.measured { return "other" }
        if id == "claude" { return "claude" }
        if id == "codex" { return "codex" }
        if id.hasPrefix("github-copilot") { return "copilot" }
        if id.hasPrefix("antigravity") { return "agy" }
        if id.hasPrefix("opencode-go") { return "go" }
        return id
    }

    /// Pools by tab: the known providers in CodexBar-like order, then any other measured pool, then Other.
    public var groups: [(id: String, pools: [Pool])] {
        var byID: [String: [Pool]] = [:]
        var seen: [String] = []
        for pool in glance.pools {
            let id = Self.groupID(pool)
            if byID[id] == nil { seen.append(id) }
            byID[id, default: []].append(pool)
        }
        let known = Self.order.filter { byID[$0] != nil }
        let extra = seen.filter { !Self.order.contains($0) && $0 != "other" }
        let tail = byID["other"] != nil ? ["other"] : []
        return (known + extra + tail).map { ($0, byID[$0] ?? []) }
    }

    public var tabs: [TabModel] {
        groups.map { group in
            let bars = group.pools.flatMap(poolBars)
            let tight = bars.max(by: { $0.used < $1.used })
            let deficit = bars.contains { $0.pace == .deficit }
            let pace: PaceKind = deficit ? .deficit : (tight?.pace ?? .unknown)
            return TabModel(
                id: group.id,
                title: Self.titles[group.id] ?? String(group.pools.first?.name.prefix(9) ?? Substring(group.id)),
                logo: group.id == "other" ? nil : group.pools.first.flatMap { ProviderLogo.forPool($0.pool) },
                used: tight?.used, pace: pace, hot: bars.contains { $0.hot })
        }
    }

    /// The tab to open on: the one in deficit or most used, else the first.
    public var defaultTab: String? {
        let ranked = tabs.enumerated().sorted { lhs, rhs in
            let l = Self.pressure(lhs.element), r = Self.pressure(rhs.element)
            return l != r ? l > r : lhs.offset < rhs.offset
        }
        if let first = ranked.first, first.element.used != nil { return first.element.id }
        return tabs.first?.id
    }

    static func pressure(_ tab: TabModel) -> Double {
        guard let used = tab.used else { return -1 }
        return (tab.pace == .deficit ? 1000 : 0) + used
    }

    // MARK: Card

    public func card(for tab: String) -> CardModel? {
        guard let group = groups.first(where: { $0.id == tab }) else { return nil }
        let multi = group.pools.count > 1
        let sections = group.pools.map { section($0, titled: multi) }
        let head = group.pools.first
        let title = multi ? (Self.cardTitles[tab] ?? Self.titles[tab] ?? tab) : (head?.label ?? head?.name ?? tab)
        let age = group.pools.compactMap { $0.staleAgeS }.max() ?? ageSeconds()
        let read = group.pools.contains { $0.measured || $0.usedPercent != nil }
        return CardModel(title: title, subtitle: Self.updated(age: age, hasReading: read),
                         badge: multi || tab == "agy" ? nil : stateWord(head?.state), sections: sections,
                         isAntigravity: tab == "agy")
    }

    func ageSeconds() -> Double? {
        guard let newest = glance.readings?.newestAgeS else { return nil }
        return newest + max(0, now.timeIntervalSince(loadedAt))
    }

    static func updated(age: Double?, hasReading: Bool) -> String {
        guard hasReading else { return "No reading" }
        guard let age else { return "Updated just now" }
        if age < 90 { return "Updated just now" }
        if age < 5400 { return "Updated \(Int((age / 60).rounded())) min ago" }
        if age < 172_800 { return "Updated \(Int((age / 3600).rounded())) h ago" }
        return "Updated \(Int((age / 86400).rounded())) days ago"
    }

    func stateWord(_ state: String?) -> String? {
        guard let state, !state.isEmpty else { return nil }
        switch state.uppercased() {
        case "GAUGE_ERROR": return "Not read"
        case "UNKNOWN": return "No quota window"
        default: return state.prefix(1).uppercased() + state.dropFirst().lowercased()
        }
    }

    func section(_ pool: Pool, titled: Bool) -> SectionModel {
        let windows = pool.limits.filter { $0.kind == "window" && $0.usedPercent != nil }
        var rows = windows.enumerated().map { index, limit in row(limit, poolID: pool.pool, index: index) }
        if rows.isEmpty, let used = pool.usedPercent {
            rows = [row(Limit(kind: "window", title: "Usage", usedPercent: used, resets: pool.resets,
                              resetAt: pool.resetAt, pace: pool.pace), poolID: pool.pool, index: 0)]
        }
        var note: String?
        var badge = titled ? stateWord(pool.state) : nil
        if rows.isEmpty {
            // Nothing to draw a bar for: say so once, with fleetctl's reason when it gave one.
            let said = pool.measured || pool.gaugeError != nil ? "Not read" : "No quota window"
            badge = titled ? said : nil
            note = pool.gaugeError.map { Self.sentence($0, limit: 90) }
                ?? (pool.measured ? "No reading yet." : "Crossfeed does not meter this pool by percent.")
        } else {
            if pool.state?.uppercased() == "UNKNOWN" { badge = nil }
            if let error = pool.gaugeError { note = "Last reading kept · " + Self.sentence(error, limit: 60) }
        }
        let title = pool.pool == "chatgpt-work" ? "ChatGPT Work" : pool.name
        return SectionModel(id: pool.pool, title: titled ? title : nil, badge: badge, note: note, rows: rows,
                            logo: ProviderLogo.forPool(pool.pool), level: pool.level.flatMap(SpendLevel.init(rawValue:)),
                            attribution: pool.levelControl, models: pool.models)
    }

    static func sentence(_ text: String, limit: Int = 96) -> String {
        let flat = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        var cut = flat
        if flat.count > limit {
            // Cut at a word, not in the middle of one.
            let head = String(flat.prefix(limit - 1))
            cut = (head.lastIndex(of: " ").map { String(head[..<$0]) } ?? head) + "…"
        }
        return cut.prefix(1).uppercased() + cut.dropFirst()
    }

    func row(_ limit: Limit, poolID: String, index: Int) -> RowModel {
        let used = min(100, max(0, limit.usedPercent ?? 0))
        var title = limit.title ?? "Usage"
        if let scope = limit.scope, !scope.isEmpty { title += " · " + scope }
        return RowModel(id: "\(poolID):\(limit.name ?? String(index))", title: title,
                        usedText: "\(Int(used.rounded()))% used",
                        resetText: resetText(limit), paceText: Self.paceLine(limit.pace),
                        bar: Self.bar(used: used, pace: limit.pace))
    }

    static func bar(used: Double, pace: Pace?) -> BarModel {
        let kind = paceKind(pace)
        let expected = pace?.elapsedPercent.map { min(100, max(0, $0)) }
        return BarModel(used: used, expected: expected, pace: kind, hot: kind == .deficit || used >= 90)
    }

    static func paceKind(_ pace: Pace?) -> PaceKind {
        switch pace?.status {
        case "deficit": return .deficit
        case "surplus": return .surplus
        case "on_pace": return .onPace
        default: return .unknown
        }
    }

    /// fleetctl's own words, once: "46% in deficit, runs out in 9h 39m" -> "Pace: 46% in deficit · runs out in 9h 39m".
    static func paceLine(_ pace: Pace?) -> String? {
        guard let words = pace?.words, !words.isEmpty else { return nil }
        return "Pace: " + words.components(separatedBy: ", ").filter { !$0.isEmpty }.joined(separator: " · ")
    }

    // MARK: Reset countdown

    static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    func resetText(_ limit: Limit) -> String? {
        if limit.resetUnknown == true { return "Reset unknown" }
        if let at = limit.resetAt, let date = Self.iso.date(from: at) { return Self.countdown(to: date, now: now) }
        if let text = limit.resets, !text.isEmpty { return "Resets " + text }
        return "Reset unknown"
    }

    public static func countdown(to date: Date, now: Date) -> String {
        let seconds = Int(date.timeIntervalSince(now))
        if seconds <= 0 { return "Resetting" }
        let days = seconds / 86400, hours = (seconds % 86400) / 3600, minutes = (seconds % 3600) / 60
        if days > 0 { return "Resets in \(days)d \(hours)h" }
        if hours > 0 { return "Resets in \(hours)h \(minutes)m" }
        return "Resets in \(max(1, minutes))m"
    }

    // MARK: Spend

    public var spend: SpendModel {
        guard let spend = glance.spend, !spend.unavailable else {
            return SpendModel(title: "Subscriptions", lines: ["Spend unavailable"], note: nil, share: nil)
        }
        var lines = spend.monthly.map { "This month: \($0.currency) \($0.total)" }
        var share: ShareModel?
        if spend.monthly.count == 1, let m = spend.monthly.first, let own = m.operator_,
           let total = Double(m.total), total > 0, let mine = Double(own) {
            let fraction = min(1, max(0, mine / total))
            share = ShareModel(title: "Your share", fraction: fraction, left: "\(m.currency) \(own) of \(m.currency) \(m.total)",
                               right: "\(Int((fraction * 100).rounded()))% yours")
        } else {
            for m in spend.monthly {
                lines.append(m.operator_.map { "Your share: \(m.currency) \($0)" } ?? "Your share: not set")
            }
        }
        var note: String?
        if spend.monthly.isEmpty {
            lines = [spend.unpriced > 0 ? "No prices yet" : "No subscriptions configured"]
            if spend.unpriced > 0 { note = "Add prices in the console" }
        } else if spend.unpriced > 0 {
            note = spend.unpriced == 1 ? "1 plan has no price yet" : "\(spend.unpriced) plans have no price yet"
        }
        return SpendModel(title: "Subscriptions", lines: lines, note: note, share: share)
    }

    // MARK: Menu bar

    func poolBars(_ pool: Pool) -> [BarModel] {
        let windows = pool.limits.filter { $0.kind == "window" && $0.usedPercent != nil }
        if windows.isEmpty, let used = pool.usedPercent, pool.measured { return [Self.bar(used: used, pace: pool.pace)] }
        return windows.map { Self.bar(used: min(100, max(0, $0.usedPercent ?? 0)), pace: $0.pace) }
    }

    public var summary: BarSummary {
        struct Ranked { var used: Double; var deficit: Double }
        let ranked: [Ranked] = glance.pools.compactMap { pool in
            guard pool.measured, let used = pool.usedPercent else { return nil }
            let points = pool.pace?.status == "deficit" ? (pool.pace?.points ?? 0) : 0
            return Ranked(used: used, deficit: Double(points))
        }.sorted { ($0.deficit, $0.used) > ($1.deficit, $1.used) }
        guard let focal = ranked.first else {
            return BarSummary(text: "", left: nil, right: nil, deficit: false, tooltip: "Crossfeed · no quota read")
        }
        let tip = glance.title.isEmpty ? "Crossfeed" : "Crossfeed · " + glance.title
        return BarSummary(text: "\(Int(focal.used.rounded()))%", left: focal.used,
                          right: ranked.count > 1 ? ranked[1].used : nil, deficit: focal.deficit > 0, tooltip: tip)
    }
}
