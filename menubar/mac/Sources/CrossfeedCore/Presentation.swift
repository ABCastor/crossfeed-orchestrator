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
    public var paceText: String?      // "46% in deficit · runs out in 9h 39m"
    public var bar: BarModel?
    /// A window the plan has that is not running yet: one line, its note where the reset would be, no bar.
    public var inactive = false
}

public struct SectionModel: Equatable, Identifiable {
    public var id: String
    public var title: String?         // pool name, shown when a tab holds several pools
    public var badge: String?         // "Conserve", "Abundant"
    public var note: String?          // why a pool is not read
    public var rows: [RowModel]
    public var logo: ProviderLogo?
    public var policy: PolicyModel?
    public var models: [String]
    public var extras: ProviderExtras
}

public struct PolicyModel: Equatable {
    public let id: String
    public let level: SpendLevel?
    public let attribution: PolicyAttribution?

    /// The only policy without a selected stop is a subscription whose pools disagree.
    public var isMixed: Bool { level == nil }

    init?(id: String, rawLevel: String?, attribution: PolicyAttribution?) {
        let level = rawLevel.flatMap(SpendLevel.init(rawValue:))
        guard level != nil || rawLevel == "mixed" else { return nil }
        self.id = id
        self.level = level
        self.attribution = attribution
    }
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
    /// The name under the icon when the strip is crowded.
    public var short: String
    public var logo: ProviderLogo?    // nil for a group without one provider (Other)
    public var used: Double?          // the tab's tightest window, for the mini bar under it
    public var pace: PaceKind
    public var hot: Bool

    public init(id: String, title: String, short: String? = nil, logo: ProviderLogo?, used: Double?, pace: PaceKind, hot: Bool) {
        self.id = id; self.title = title; self.short = short ?? title; self.logo = logo; self.used = used; self.pace = pace; self.hot = hot
    }
}

/// One provider in the Overview tab.
public struct OverviewRow: Equatable, Identifiable {
    /// What wants a look: a provider that is used up or has a major incident (terracotta), or a minor one (olive).
    public enum Flag: Equatable { case none, minor, severe, usedUp }
    public var id: String
    public var title: String
    public var logo: ProviderLogo?
    public var used: Double?
    public var window: String         // "Week", "5h"
    public var usedText: String       // "50%", "Used up", "Not read"
    public var reset: String?         // "4d 5h", or the clock time
    public var bar: BarModel?
    public var flag: Flag
}

public struct CardModel: Equatable {
    public var title: String
    public var subtitle: String       // "Updated 2 min ago"
    public var badge: String?
    public var sections: [SectionModel]
    public var isAntigravity: Bool
    public var subscriptionPolicy: PolicyModel?
    /// The pool the console's /usage page opens at for this tab.
    public var usagePool: String?

    public var heading: String { isAntigravity ? "Google AI Pro" : title }

    /// The same rows as Claude, with the allowance name beside its window and no pool-level dial.
    public var allowanceSections: [SectionModel] {
        guard isAntigravity else { return sections }
        let order = ["antigravity-gemini", "antigravity-3p", "antigravity-argon"]
        let titles = ["antigravity-gemini": "Gemini", "antigravity-3p": "Claude/GPT", "antigravity-argon": "Argon"]
        return sections.sorted { (order.firstIndex(of: $0.id) ?? order.count) < (order.firstIndex(of: $1.id) ?? order.count) }
            .map { section in
                guard let family = titles[section.id] else { return section }
                var result = section
                result.title = nil
                let expected = section.id == "antigravity-gemini" ? ["weekly", "5-hour"] : ["weekly"]
                result.rows = expected.map { window in
                    let title = section.id == "antigravity-argon" ? "Weekly · Argon only" : "\(family) \(window)"
                    if var row = section.rows.first(where: { $0.title.lowercased().hasPrefix(window) }) {
                        row.title = title
                        return row
                    }
                    return RowModel(id: section.id + ":unread-" + window, title: title,
                                    usedText: "Not read", resetText: "Reset unknown", paceText: nil, bar: nil)
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
    /// One line per plan when the glance lists them: ("Claude Max 20x", "£183/mo"). A plan with no price says so.
    public var plans: [PlanLine] = []
}

public struct PlanLine: Equatable {
    public var name: String
    public var price: String          // "£183/mo", "No price"
    public var priced: Bool
}

/// What the menu bar item draws: an icon (the two tanks, or the provider's logo) and a line of text that
/// says which allowance the number belongs to.
public struct BarSummary: Equatable {
    public var text: String           // "Gemini 5h 59%", or "" when nothing is read or the text is off
    public var left: Double?          // left tank: the shown provider's short window (5-hour, session); nil draws an empty outline
    public var right: Double?         // right tank: the same provider's weekly window; nil draws an empty outline
    public var deficit: Bool
    public var tooltip: String
    public var logo: ProviderLogo?    // the focal provider's mark, when the icon is the logo
    public var icon: BarPrefs.Icon
    public var name: String           // "Gemini"
    public var window: String         // "5h"
    public var used: Double?          // used %, whatever the count
    public var reset: String?         // "2h 13m": when the window resets, nil when unknown
    public var pace: String?          // "46% over", "81% under", "on pace", nil without a pace
    public var cost: String?          // "US$11.26 today": estimated cost across providers, nil when none reports one

    public init(text: String, left: Double?, right: Double?, deficit: Bool, tooltip: String,
                logo: ProviderLogo? = nil, icon: BarPrefs.Icon = .tanks, name: String = "", window: String = "", used: Double? = nil,
                reset: String? = nil, pace: String? = nil, cost: String? = nil) {
        self.text = text; self.left = left; self.right = right; self.deficit = deficit; self.tooltip = tooltip
        self.logo = logo; self.icon = icon; self.name = name; self.window = window; self.used = used
        self.reset = reset; self.pace = pace; self.cost = cost
    }
}

public struct Presenter {
    public let glance: Glance
    public let loadedAt: Date
    public let now: Date

    /// Write a reset as the clock time it happens ("Resets Mon 20:56") instead of a countdown.
    public let resetClock: Bool
    let timeZone: TimeZone
    /// The operator's budgets and chart options (panel.json). The menu only shows them; nothing here enforces a budget.
    public var panel = PanelPrefs()

    public init(glance: Glance, loadedAt: Date, now: Date, resetClock: Bool = false, timeZone: TimeZone = .current) {
        self.glance = glance
        self.loadedAt = loadedAt
        self.now = now
        self.resetClock = resetClock
        self.timeZone = timeZone
    }

    // MARK: Tabs

    static let order = ["claude", "codex", "copilot", "agy", "go"]
    static let titles = ["claude": "Claude", "codex": "Codex", "copilot": "Copilot", "agy": "AGY",
                         "go": "OpenCode", "other": "Other",
                         "openrouter-free": "OpenRouter", "gemini-metered": "Gemini API", "chatgpt-work": "Crossfeed Chat"]
    /// What the tab strip prints when it has no room for the full name.
    static let shortTitles = ["openrouter-free": "Router", "gemini-metered": "Gemini", "chatgpt-work": "Chat", "go": "OpenCode"]

    /// A pool with no percentage window that is billed per token, by prepaid credit or through a relay: its own tab.
    static func isAPI(_ pool: Pool) -> Bool {
        !pool.measured && (pool.api != nil || PanelPrefs.apiTabs.contains(pool.pool))
    }
    static let cardTitles = ["agy": "Antigravity"]

    static func groupID(_ pool: Pool) -> String {
        let id = pool.pool
        if pool.subscription == "antigravity" || ["antigravity-gemini", "antigravity-3p", "antigravity-argon"].contains(id) { return "agy" }
        if isAPI(pool) { return id }
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
        let rank = { (id: String) in PanelPrefs.apiTabs.firstIndex(of: id) ?? PanelPrefs.apiTabs.count }
        let extra = seen.filter { !Self.order.contains($0) && $0 != "other" }.enumerated()
            .sorted { (rank($0.element), $0.offset) < (rank($1.element), $1.offset) }.map(\.element)
        let tail = byID["other"] != nil ? ["other"] : []
        return (known + extra + tail).map { ($0, byID[$0] ?? []) }
    }

    public var tabs: [TabModel] {
        groups.map { group in
            var bars = group.pools.flatMap(poolBars)
            if bars.isEmpty, let pool = group.pools.first, Self.isAPI(pool), let spend = apiSpend(pool) {
                bars = spend.tabBar.map { [$0] } ?? []
            }
            let tight = bars.max(by: { $0.used < $1.used })
            let deficit = bars.contains { $0.pace == .deficit }
            let pace: PaceKind = deficit ? .deficit : (tight?.pace ?? .unknown)
            let title = Self.titles[group.id] ?? String(group.pools.first?.name.prefix(9) ?? Substring(group.id))
            return TabModel(
                id: group.id, title: title, short: Self.shortTitles[group.id] ?? title,
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
        let agy = tab == "agy"
        // Antigravity's three allowances share one status feed: say an incident once, on the section drawn first.
        let shown = ["antigravity-gemini", "antigravity-3p", "antigravity-argon"]
        let firstShown = group.pools.filter { $0.incident != nil }.min { (shown.firstIndex(of: $0.pool) ?? shown.count) < (shown.firstIndex(of: $1.pool) ?? shown.count) }?.pool
        let sections = group.pools.map { pool in
            section(pool, titled: multi, hasOwnPolicy: !agy, showIncident: !agy || pool.pool == firstShown)
        }
        let subscriptionPolicy = agy ? glance.subscriptions.first(where: { $0.id == "antigravity" }).flatMap { subscription in
            let attribution = subscription.levelControl ?? group.pools.compactMap(\.levelControl)
                .compactMap { record in Self.controlDate(record.when).map { ($0, record) } }
                .max { $0.0 < $1.0 }?.1
            return PolicyModel(id: subscription.id, rawLevel: subscription.level, attribution: attribution)
        } : nil
        let head = group.pools.first
        let title = multi ? (Self.cardTitles[tab] ?? Self.titles[tab] ?? tab) : (head?.label ?? head?.name ?? tab)
        let age = group.pools.compactMap { $0.staleAgeS }.max() ?? ageSeconds()
        let read = group.pools.contains { $0.measured || $0.usedPercent != nil }
        return CardModel(title: title, subtitle: Self.updated(age: age, hasReading: read),
                         badge: multi || agy ? nil : stateWord(head?.state), sections: sections,
                         isAntigravity: agy, subscriptionPolicy: subscriptionPolicy,
                         usagePool: agy ? "antigravity" : head?.pool)
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

    func section(_ pool: Pool, titled: Bool, hasOwnPolicy: Bool, showIncident: Bool = true) -> SectionModel {
        let windows = pool.limits.filter { $0.kind == "window" }
        var rows = windows.enumerated().map { index, limit in row(limit, poolID: pool.pool, index: index) }
        if rows.isEmpty, let used = pool.usedPercent {
            rows = [row(Limit(kind: "window", title: "Usage", usedPercent: used, resets: pool.resets,
                              resetAt: pool.resetAt, pace: pool.pace), poolID: pool.pool, index: 0)]
        }
        var note: String?
        var badge = titled ? stateWord(pool.state) : nil
        if rows.allSatisfy({ $0.bar == nil && !$0.inactive }) {
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
                            logo: ProviderLogo.forPool(pool.pool),
                            policy: hasOwnPolicy ? PolicyModel(id: pool.pool, rawLevel: pool.level, attribution: pool.levelControl) : nil,
                            models: Self.modelNames(pool.models), extras: ProviderExtras(pool: pool, showIncident: showIncident))
    }

    static func modelNames(_ models: [GlanceModel]) -> [String] {
        let active = models.filter { !$0.archived }
        func family(_ model: GlanceModel) -> String { model.family ?? model.name }
        let costs = Dictionary(grouping: active, by: family).mapValues { $0.compactMap(\.typicalCostUSD).max() ?? -.infinity }
        // Preserve legacy order for families whose cost is not reported; tie costs in input order.
        var families = [String: Int]()
        for model in active where families[family(model)] == nil { families[family(model)] = families.count }
        return active.enumerated().sorted { lhs, rhs in
            let l = family(lhs.element), r = family(rhs.element)
            if l != r {
                if costs[l] != costs[r] { return costs[l, default: -.infinity] > costs[r, default: -.infinity] }
                return families[l, default: 0] < families[r, default: 0]
            }
            let lc = lhs.element.typicalCostUSD ?? -.infinity, rc = rhs.element.typicalCostUSD ?? -.infinity
            return lc != rc ? lc > rc : lhs.offset < rhs.offset
        }.map { $0.element.name }
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
        let used = limit.usedPercent.map { min(100, max(0, $0)) }
        var title = limit.title ?? "Usage"
        if let scope = limit.scope, !scope.isEmpty { title += " · " + scope }
        if !limit.isActive {
            return RowModel(id: "\(poolID):\(limit.name ?? String(index))", title: title, usedText: "",
                            resetText: limit.note ?? "Not started", paceText: nil, bar: nil, inactive: true)
        }
        return RowModel(id: "\(poolID):\(limit.name ?? String(index))", title: title,
                        usedText: used.map { $0 >= Self.usedUp ? "Used up" : "\(Int($0.rounded()))% used" } ?? "Not read",
                        resetText: resetText(limit), paceText: Self.paceLine(limit.pace),
                        bar: used.map { Self.bar(used: $0, pace: limit.pace) })
    }

    /// From here an allowance reads as used up, not as a percent.
    static let usedUp = 99.5

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

    /// fleetctl's own words, once: "46% in deficit, runs out in 9h 39m" -> "46% in deficit · runs out in 9h 39m".
    /// The dot beside it says it is the pace; the word does not repeat.
    static func paceLine(_ pace: Pace?) -> String? {
        guard let words = pace?.words, !words.isEmpty else { return nil }
        let line = words.components(separatedBy: ", ").filter { !$0.isEmpty }.joined(separator: " · ")
        return line.prefix(1).uppercased() + line.dropFirst()
    }

    // MARK: Reset countdown

    static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static let isoFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    public static func controlDate(_ value: String) -> Date? {
        iso.date(from: value) ?? isoFractional.date(from: value)
    }

    func resetText(_ limit: Limit) -> String? {
        if limit.resetUnknown == true { return "Reset unknown" }
        if let at = limit.resetAt, let date = Self.iso.date(from: at) {
            return resetClock ? "Resets " + clockText(date, today: "today ") : Self.countdown(to: date, now: now)
        }
        if let text = limit.resets, !text.isEmpty { return "Resets " + text }
        return "Reset unknown"
    }

    /// "today 14:13", "tomorrow 09:00", "Mon 20:56" within a week, else "5 Dec 09:00", in the viewer's time zone.
    func clockText(_ date: Date, today: String) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let time = DateFormatter()
        time.locale = Locale(identifier: "en_GB"); time.timeZone = timeZone
        time.dateFormat = "HH:mm"
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: now), to: calendar.startOfDay(for: date)).day ?? 0
        if days <= 0 { return today + time.string(from: date) }
        if days == 1 { return "tomorrow " + time.string(from: date) }
        time.dateFormat = days < 7 ? "EEE HH:mm" : "d MMM HH:mm"
        return time.string(from: date)
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
        let plans = spend.plans.map { plan in
            Self.planPrice(plan).map { PlanLine(name: plan.name, price: $0, priced: true) }
                ?? PlanLine(name: plan.name, price: "No price", priced: false)
        }
        if !plans.isEmpty {
            // Each plan names its own price, so the lines shrink to one total and a share only where one is set.
            lines = spend.monthly.isEmpty ? [] : ["Per month: " + spend.monthly.map { "\($0.currency) \($0.total)" }.joined(separator: " · ")]
            lines += spend.monthly.filter { $0.operator_ != nil }.map { "Your share: \($0.currency) \($0.operator_ ?? "")" }
            note = nil
        }
        return SpendModel(title: "Subscriptions", lines: lines, note: note, share: share, plans: plans)
    }

    /// "£183/mo", "€20/mo", "US$120/yr": the plan's own amount and period, or nil without an amount.
    static func planPrice(_ plan: PlanPrice) -> String? {
        guard let raw = plan.amount, let value = Double(raw), value >= 0 else { return nil }
        let symbols = ["GBP": "£", "EUR": "€", "USD": "US$"]
        let code = (plan.currency ?? "").uppercased()
        let prefix = symbols[code] ?? (code.isEmpty ? "" : code + " ")
        let number = value == value.rounded() ? String(Int(value)) : String(format: "%.2f", value)
        let periods = ["month": "/mo", "monthly": "/mo", "year": "/yr", "yearly": "/yr", "annual": "/yr", "week": "/wk", "weekly": "/wk"]
        return prefix + number + (periods[(plan.period ?? "month").lowercased()] ?? "/mo")
    }

    // MARK: Overview

    public static let overviewID = "overview"

    /// One compact row per provider, in the given order: its tightest window, when that resets, and a dot when
    /// something needs a look. Tapping a row opens that provider's tab.
    public func overviewRows(order: [String]) -> [OverviewRow] {
        let byID = Dictionary(uniqueKeysWithValues: groups.map { ($0.id, $0.pools) })
        let titles = Dictionary(uniqueKeysWithValues: tabs.map { ($0.id, $0) })
        return order.compactMap { id in
            guard let pools = byID[id], let tab = titles[id] else { return nil }
            let row = overviewRow(tab: tab, pools: pools)
            // "Other" holds pools with no percentage; with nothing to say it has no row.
            return id == "other" && row.used == nil && row.flag == .none ? nil : row
        }
    }

    func overviewRow(tab: TabModel, pools: [Pool]) -> OverviewRow {
        let live = pools.filter { !$0.isRetired }
        let shown = live.isEmpty ? pools : live
        var candidates: [(limit: Limit?, used: Double, pace: Pace?)] = []
        for pool in shown {
            let windows = pool.limits.filter { $0.kind == "window" && $0.usedPercent != nil && $0.isActive }
            if windows.isEmpty, pool.limits.isEmpty, let used = pool.usedPercent, pool.measured {
                candidates.append((nil, used, pool.pace))
            }
            candidates += windows.map { ($0, min(100, max(0, $0.usedPercent ?? 0)), $0.pace) }
        }
        let tight = candidates.max { lhs, rhs in
            (lhs.pace?.status == "deficit" ? 1 : 0, lhs.used) < (rhs.pace?.status == "deficit" ? 1 : 0, rhs.used)
        } ?? candidates.max { $0.used < $1.used }
        let incident = shown.compactMap(\.incident).first { ["maintenance", "minor", "major", "critical"].contains($0.level) }
        var flag = OverviewRow.Flag.none
        if let incident { flag = incident.level == "major" || incident.level == "critical" ? .severe : .minor }
        guard let tight else {
            return OverviewRow(id: tab.id, title: tab.title, logo: tab.logo, used: nil, window: "", usedText: "Not read",
                               reset: nil, bar: nil, flag: flag)
        }
        let usedUp = tight.used >= Self.usedUp
        if usedUp { flag = .usedUp }
        let date = tight.limit?.resetAt.flatMap { Self.iso.date(from: $0) }
        let reset = date.map { resetClock ? clockText($0, today: "") : Self.shortCountdown(to: $0, now: now) }
        return OverviewRow(id: tab.id, title: tab.title, logo: tab.logo, used: tight.used,
                           window: Self.barWindow(tight.limit).short,
                           usedText: usedUp ? "Used up" : "\(Int(tight.used.rounded()))%",
                           reset: reset, bar: Self.bar(used: tight.used, pace: tight.pace), flag: flag)
    }

    // MARK: Menu bar

    func poolBars(_ pool: Pool) -> [BarModel] {
        let windows = pool.limits.filter { $0.kind == "window" && $0.usedPercent != nil && $0.isActive }
        if windows.isEmpty, pool.limits.isEmpty, let used = pool.usedPercent, pool.measured { return [Self.bar(used: used, pace: pool.pace)] }
        return windows.map { Self.bar(used: min(100, max(0, $0.usedPercent ?? 0)), pace: $0.pace) }
    }

    /// The default look: the most pressed allowance, named.
    public var summary: BarSummary { barSummary(BarPrefs()) }

    struct Pressed {
        var pool: Pool
        var limit: Limit?
        var used: Double
        var deficit: Double
    }

    /// Measured pools, most pressed first: a pool in deficit leads, then the most used. A chosen window ranks
    /// the pools that have it by that window; a window nobody has falls back to each pool's tightest.
    func pressedPools(window: String = BarPrefs.tightest) -> [Pressed] {
        func windows(_ pool: Pool) -> [Limit] { pool.limits.filter { $0.kind == "window" && $0.usedPercent != nil && $0.isActive } }
        // A pool whose models are all archived or withdrawn (Argon, from 1 Dec) never leads the bar.
        let live = glance.pools.filter { !$0.isRetired }.isEmpty ? glance.pools : glance.pools.filter { !$0.isRetired }
        func points(_ pace: Pace?) -> Double { pace?.status == "deficit" ? Double(pace?.points ?? 0) : 0 }
        let chosen: [Pressed] = window == BarPrefs.tightest ? [] : live.compactMap { pool in
            guard pool.measured, let hit = windows(pool).first(where: { Self.barWindow($0).short == window && ($0.scope ?? "").isEmpty })
                    ?? windows(pool).first(where: { Self.barWindow($0).short == window }),
                  let used = hit.usedPercent else { return nil }
            return Pressed(pool: pool, limit: hit, used: used, deficit: points(hit.pace))
        }
        let all: [Pressed] = chosen.isEmpty ? live.compactMap { pool in
            guard pool.measured, let used = pool.usedPercent else { return nil }
            let match = windows(pool).filter { abs(($0.usedPercent ?? -1) - used) < 0.5 }
            let limit = match.first { ($0.scope ?? "").isEmpty } ?? match.first
                ?? windows(pool).max { ($0.usedPercent ?? 0) < ($1.usedPercent ?? 0) }
            return Pressed(pool: pool, limit: limit, used: used, deficit: points(pool.pace))
        } : chosen
        return all.sorted { ($0.deficit, $0.used) > ($1.deficit, $1.used) }
    }

    static let agyNames = ["antigravity-gemini": "Gemini", "antigravity-3p": "Claude/GPT", "antigravity-argon": "Argon"]

    /// "Gemini", "Claude", "OpenCode": the short name of the allowance a bar number belongs to.
    static func barName(_ pool: Pool) -> String {
        if let name = agyNames[pool.pool] { return name }
        let tab = groupID(pool)
        if tab == "other" || titles[tab] == nil { return String(pool.name.prefix(14)) }
        return titles[tab] ?? pool.name
    }

    /// ("5h", "5-hour") for a window: by its length when fleetctl sends one, else by its title.
    static func barWindow(_ limit: Limit?) -> (short: String, long: String) {
        if let m = limit?.minutes {
            if m == 300 { return ("5h", "5-hour") }
            if m == 1440 { return ("Day", "daily") }
            if m == 10080 { return ("Week", "weekly") }
            if m >= 40000 && m <= 45000 { return ("Month", "monthly") }
            if m < 1440 { return ("\(Int((m / 60).rounded()))h", "\(Int((m / 60).rounded()))-hour") }
        }
        let title = (limit?.title ?? "").lowercased()
        if title.contains("5-hour") || title.contains("5h") { return ("5h", "5-hour") }
        if title.contains("week") { return ("Week", "weekly") }
        if title.contains("month") { return ("Month", "monthly") }
        if title.contains("day") { return ("Day", "daily") }
        if title.contains("session") { return ("Session", "session") }
        let word = (limit?.title ?? "Usage").split(separator: " ").first.map(String.init) ?? "Usage"
        return (String(word.prefix(8)), word.lowercased())
    }

    /// The providers the bar can follow: those with a percent to show right now, in tab order.
    public var barChoices: [(id: String, title: String)] {
        let readable = Set(pressedPools().map { Self.groupID($0.pool) })
        return tabs.filter { readable.contains($0.id) }.map { ($0.id, $0.title) }
    }

    /// "Sun 29 Nov 12:04": the full day and time, for an allowance that is used up.
    func fullClock(_ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_GB"); f.timeZone = timeZone
        f.dateFormat = "EEE d MMM HH:mm"
        return f.string(from: date)
    }

    /// The tanks: this pool's short window (5-hour, session) on the left, its weekly window on the right.
    /// A window the plan lacks, or one that is not running, is nil: an empty outline.
    func tankWindows(_ pool: Pool) -> (short: Limit?, weekly: Limit?) {
        let windows = pool.limits.filter { $0.kind == "window" && $0.isActive && $0.usedPercent != nil && ($0.scope ?? "").isEmpty }
        func pick(_ names: Set<String>) -> Limit? { windows.first { names.contains(Self.barWindow($0).short) } }
        return (pick(["5h", "Session"]) ?? windows.first { ($0.minutes ?? 99999) < 1440 }, pick(["Week"]))
    }

    public func barSummary(_ chosen: BarPrefs) -> BarSummary {
        let prefs = chosen.effective
        let ranked = pressedPools(window: prefs.window)
        // A provider that has no reading right now falls back to the most pressed allowance; the text names it.
        let focal = (prefs.show == BarPrefs.pressed ? nil : ranked.first { Self.groupID($0.pool) == prefs.show }) ?? ranked.first
        guard let focal else {
            return BarSummary(text: "", left: nil, right: nil, deficit: false, tooltip: "Crossfeed · no quota read", icon: .tanks)
        }
        let name = Self.barName(focal.pool)
        let window = Self.barWindow(focal.limit)
        let tanks = tankWindows(focal.pool)
        let fill = { (limit: Limit?) in limit?.usedPercent.map { prefs.count == .used ? $0 : 100 - $0 } }
        let resetDate = focal.limit?.resetAt.flatMap { Self.iso.date(from: $0) }
        var sentence = "\(name) \(window.long): \(Int(focal.used.rounded()))% used"
        if focal.used >= Self.usedUp {
            sentence = "\(name) used up" + (resetDate.map { " · resets " + fullClock($0) } ?? "")
        } else if let limit = focal.limit, let reset = resetText(limit), reset != "Reset unknown" {
            sentence += " · " + reset
        }
        let logo = ProviderLogo.forPool(focal.pool.pool)
        let reset = resetDate.map { resetClock ? clockText($0, today: "") : Self.shortCountdown(to: $0, now: now) }
        let pace = Self.shortPace(focal.limit?.pace ?? focal.pool.pace)
        let spent = glance.pools.compactMap { $0.usageCost?.todayUSD }
        let cost = spent.isEmpty ? nil : String(format: "US$%.2f today", spent.reduce(0, +))
        var text = BarPrefs.format(name: name, window: window.short, used: focal.used, count: prefs.count, text: prefs.text)
        let extra: String? = [.reset: reset, .pace: pace, .cost: cost][prefs.extra] ?? nil
        if !text.isEmpty, let extra { text += " · " + extra }
        return BarSummary(
            text: text,
            left: fill(tanks.short), right: fill(tanks.weekly), deficit: focal.deficit > 0,
            tooltip: sentence, logo: logo, icon: prefs.icon == .logo && logo == nil ? .tanks : prefs.icon,
            name: name, window: window.short, used: focal.used, reset: reset, pace: pace, cost: cost)
    }

    /// "4d 8h", "2h 13m", "13m": the countdown without its sentence, for the bar.
    static func shortCountdown(to date: Date, now: Date) -> String {
        let seconds = Int(date.timeIntervalSince(now))
        if seconds <= 0 { return "now" }
        let days = seconds / 86400, hours = (seconds % 86400) / 3600, minutes = (seconds % 3600) / 60
        if days > 0 { return "\(days)d \(hours)h" }
        if hours > 0 { return "\(hours)h \(minutes)m" }
        return "\(max(1, minutes))m"
    }

    /// How far a window runs ahead of (over) or behind (under) an even burn.
    static func shortPace(_ pace: Pace?) -> String? {
        switch pace?.status {
        case "deficit": return pace?.points.map { "\($0)% over" }
        case "surplus": return pace?.points.map { "\($0)% under" }
        case "on_pace": return "on pace"
        default: return nil
        }
    }
}
