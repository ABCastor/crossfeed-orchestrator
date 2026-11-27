// Crossfeed menu: what the B+ tiles draw. Pure functions of (Glance, now, Used/Left): every word, bar segment and
// colour decision is made here so the tests can read it. Fewest words: a figure, the window, a pace word.
import Foundation

public enum TileID {
    public static let limits = "limits", resets = "resets", tokens = "tokens", policy = "policy", credits = "credits", links = "links"
    public static let alert = "alert", providers = "providers", subscriptions = "subscriptions"
    public static func provider(_ id: String) -> String { "provider:" + id }
    public static func isPlugin(_ id: String) -> Bool { id.hasPrefix("plugin:") }
}

/// One stretch of a pace bar, in percent of its length. Deficit and surplus are both drawn as stripes.
public struct BarSegment: Equatable {
    public enum Kind: Equatable { case fill, deficit, surplus }
    public var from: Double, to: Double
    public var kind: Kind
    public init(from: Double, to: Double, kind: Kind) { self.from = from; self.to = to; self.kind = kind }
}

public struct PaceBarModel: Equatable {
    public var segments: [BarSegment]
    /// Where an even burn would be by now (in Used mode: the share of the window that has passed).
    public var tick: Double?
    public init(segments: [BarSegment], tick: Double?) { self.segments = segments; self.tick = tick }

    /// The bar for `used` percent with a pace tick at `elapsed`. Used mode grows from the left with what is spent;
    /// Left mode shows what remains. Past the tick the stripes are terracotta (deficit); short of it they are green
    /// (surplus): from the fill's end exactly to the tick, the full height of the bar.
    public static func make(used: Double, elapsed: Double?, show: PanelPrefs.Show, full: Bool) -> PaceBarModel {
        let u = min(100, max(0, used))
        let left = show == .left
        let v = left ? 100 - u : u
        if full { return PaceBarModel(segments: left ? [] : [BarSegment(from: 0, to: 100, kind: .deficit)], tick: nil) }
        guard let elapsed else { return PaceBarModel(segments: [BarSegment(from: 0, to: v, kind: .fill)], tick: nil) }
        let e = min(100, max(0, elapsed))
        let t = left ? 100 - e : e
        let deficit = u > e
        var segments: [BarSegment] = []
        if deficit {
            if left {
                segments = [BarSegment(from: 0, to: v, kind: .fill), BarSegment(from: v, to: t, kind: .deficit)]
            } else {
                segments = [BarSegment(from: 0, to: t, kind: .fill), BarSegment(from: t, to: v, kind: .deficit)]
            }
        } else {
            if left {
                segments = [BarSegment(from: 0, to: t, kind: .fill), BarSegment(from: t, to: v, kind: .surplus)]
            } else {
                segments = [BarSegment(from: 0, to: v, kind: .fill), BarSegment(from: v, to: t, kind: .surplus)]
            }
        }
        return PaceBarModel(segments: segments.filter { $0.to > $0.from }, tick: t)
    }
}

public struct PaceWord: Equatable {
    public enum Kind: Equatable { case deficit, surplus, onPace }
    public var text: String
    public var kind: Kind
    public init(text: String, kind: Kind) { self.text = text; self.kind = kind }
}

/// One window: the figure, the window named with its reset, the bar, and how it runs against an even burn.
public struct LimitLine: Equatable, Identifiable {
    public var id: String
    public var figure: String            // "58% used", "42% left", "Used up", "Not read"
    public var hot: Bool
    public var window: String            // "Weekly", "Fable weekly"
    public var resets: String            // "resets Tue 14:00", "reset time unknown"
    public var bar: PaceBarModel
    public var pace: PaceWord?           // "17% deficit", "25% surplus"
    public var runsOut: String?          // "Runs out in 2d 1h"
    public var used: Double
    public var full: Bool

    public init(id: String, figure: String, hot: Bool, window: String, resets: String, bar: PaceBarModel, pace: PaceWord?,
                runsOut: String?, used: Double, full: Bool) {
        self.id = id; self.figure = figure; self.hot = hot; self.window = window; self.resets = resets; self.bar = bar
        self.pace = pace; self.runsOut = runsOut; self.used = used; self.full = full
    }

    public var accessibility: String { [figure, window, resets, pace?.text, runsOut].compactMap { $0 }.joined(separator: ", ") }
}

public struct LimitGroup: Equatable, Identifiable {
    public var id: String
    public var title: String?            // "Gemini", "Claude and GPT"
    public var note: String?             // "Shares the Gemini 5-hour limit"
    public var lines: [LimitLine]
}

public struct ResetRow: Equatable, Identifiable {
    public var id: String
    public var name: String              // "Weekly", "Fable", "Limit"
    public var next: Bool
    public var until: String?            // "10 Dec"
    public var key: String?
    public var canUse: Bool
    public var ask: String               // "Use the weekly reset?"
    public var effect: String?           // "Weekly 58% → 0% · Fable 16% → 0%"
}

public struct TokensModel: Equatable {
    public var today: String             // "958M"
    public var days: [Int64]
}

public struct CreditsModel: Equatable {
    public var amount: String            // "11,872 credits"
    public var until: String?            // "until 1 Jan"
}

public struct LinkItem: Equatable, Identifiable {
    public enum Kind: Equatable { case usage(String?), status(URL), console }
    public var title: String
    public var kind: Kind
    public var id: String { title }
}

public struct IncidentNote: Equatable {
    public var word: String              // "Minor issue", "Major outage"
    public var text: String
    public var severe: Bool
}

public struct HeaderBadge: Equatable {
    public var word: String
    public var hot: Bool
}

/// A provider tab: a header, then the tiles the operator chose, each with what it draws.
public struct ProviderPage: Equatable {
    public var id: String
    public var title: String
    public var plan: String?
    public var logo: ProviderLogo?
    public var updated: String
    public var badge: HeaderBadge?
    public var incident: IncidentNote?
    public var groups: [LimitGroup]
    public var notMetered: [String]
    public var resets: [ResetRow]
    public var tokens: TokensModel?
    public var policy: PolicyModel?
    public var credits: CreditsModel?
    public var links: [LinkItem]

    /// The built-in tiles that have something to show now.
    public var available: [String] {
        var ids = [TileID.limits]
        if !resets.isEmpty { ids.append(TileID.resets) }
        if tokens != nil { ids.append(TileID.tokens) }
        if policy != nil { ids.append(TileID.policy) }
        if credits != nil { ids.append(TileID.credits) }
        ids.append(TileID.links)
        return ids
    }
}

/// A provider on Overview: its tightest window, in the same parts as a limit row.
public struct ProviderCard: Equatable, Identifiable {
    public var id: String
    public var title: String
    public var logo: ProviderLogo?
    public var tickets: Int
    public var line: LimitLine?
    public var window: String
}

public struct SubscriptionRow: Equatable, Identifiable {
    public var id: String
    public var name: String
    public var share: String?            // "½"
    public var full: String?             // "£183", shown when the plan is split
    public var yours: String             // "£183", "$100", "No price"
    public var priced: Bool
}

public struct SubscriptionsModel: Equatable {
    public var rows: [SubscriptionRow]
    public var total: String?            // "≈ $348"
}

public struct OverviewPage: Equatable {
    public struct Alert: Equatable { public var text: String; public var hot: Bool }
    public var alert: Alert?
    public var providers: [ProviderCard]
    public var subscriptions: SubscriptionsModel?
    public var tokens: TokensModel?
    public var links: [LinkItem]
}

// MARK: Building them

extension Presenter {
    // MARK: Words

    /// "14:00" today, "Tue 14:00" within a week, else "10 Dec": when a window resets, in the viewer's time zone.
    public func whenText(_ date: Date) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_GB"); f.timeZone = timeZone
        if calendar.isDate(date, inSameDayAs: now) { f.dateFormat = "HH:mm" }
        else if date.timeIntervalSince(now) < 6.5 * 86400, date > now { f.dateFormat = "EEE HH:mm" }
        else { f.dateFormat = "d MMM" }
        return f.string(from: date)
    }

    func dayText(_ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_GB"); f.timeZone = timeZone
        f.dateFormat = "d MMM"
        return f.string(from: date)
    }

    static func span(_ seconds: Double) -> String {
        let s = Int(seconds), d = s / 86400, h = (s % 86400) / 3600, m = (s % 3600) / 60
        return d > 0 ? "\(d)d \(h)h" : (h > 0 ? "\(h)h \(m)m" : "\(max(1, m))m")
    }

    /// 958000542 -> "958M", 1.2e9 -> "1.2B", 12345 -> "12k".
    public static func big(_ n: Int64) -> String {
        let v = Double(n)
        func trim(_ x: Double) -> String { let s = String(format: "%.1f", x); return s.hasSuffix(".0") ? String(s.dropLast(2)) : s }
        if v >= 1e9 { return (v >= 1e10 ? String(Int((v / 1e9).rounded())) : trim(v / 1e9)) + "B" }
        if v >= 1e6 { return "\(Int((v / 1e6).rounded()))M" }
        if v >= 1e3 { return "\(Int((v / 1e3).rounded()))k" }
        return String(n)
    }

    // MARK: Limits

    func limitLine(_ limit: Limit, id: String, show: PanelPrefs.Show) -> LimitLine {
        let scope = limit.scope.map { $0.hasSuffix(" only") ? String($0.dropLast(5)) : $0 }.flatMap { $0.isEmpty ? nil : $0 }
        let title = limit.title ?? "Usage"
        let window = scope.map { "\($0) \(title.lowercased())" } ?? title
        let resets: String
        if limit.resetUnknown != true, let at = limit.resetAt, let date = Self.controlDate(at) { resets = "resets " + whenText(date) }
        else { resets = "reset time unknown" }
        guard let raw = limit.usedPercent else {
            return LimitLine(id: id, figure: "Not read", hot: false, window: window, resets: resets,
                             bar: PaceBarModel(segments: [], tick: nil), pace: nil, runsOut: nil, used: 0, full: false)
        }
        let used = min(100, max(0, raw))
        let full = used >= Self.usedUp
        let elapsed = full ? nil : limit.pace?.elapsedPercent
        var pace: PaceWord?
        if let elapsed {
            let delta = used - elapsed
            pace = abs(delta) < 1 ? PaceWord(text: "on pace", kind: .onPace)
                : PaceWord(text: "\(Int(abs(delta).rounded()))% \(delta > 0 ? "deficit" : "surplus")", kind: delta > 0 ? .deficit : .surplus)
        }
        var runsOut: String?
        if !full, limit.pace?.status == "deficit", let s = limit.pace?.runsOutInS, s > 0 { runsOut = "Runs out in " + Self.span(s) }
        let figure = full ? "Used up" : (show == .used ? "\(Int(used.rounded()))% used" : "\(Int((100 - used).rounded()))% left")
        return LimitLine(id: id, figure: figure, hot: full || runsOut != nil || used >= 90, window: window, resets: resets,
                         bar: PaceBarModel.make(used: used, elapsed: elapsed, show: show, full: full),
                         pace: full ? nil : pace, runsOut: runsOut, used: used, full: full)
    }

    /// Only windows the provider reports: a placeholder ("Starts with your next message") is never a row.
    func limitLines(_ pool: Pool, show: PanelPrefs.Show) -> [LimitLine] {
        var windows = pool.limits.filter { $0.kind == "window" && $0.isActive }
        if windows.isEmpty, pool.limits.isEmpty, pool.measured, let used = pool.usedPercent {
            windows = [Limit(kind: "window", title: "Usage", usedPercent: used, resets: pool.resets, resetAt: pool.resetAt, pace: pool.pace)]
        }
        return windows.enumerated().sorted { ($0.element.minutes ?? 1e9, $0.offset) < ($1.element.minutes ?? 1e9, $1.offset) }
            .map { limitLine($0.element, id: "\(pool.pool):\($0.element.name ?? String($0.offset))", show: show) }
    }

    // MARK: A provider tab

    static let agyTitles = ["antigravity-gemini": "Gemini", "antigravity-3p": "Claude and GPT", "antigravity-argon": "Argon"]

    func planLabel(_ pools: [Pool], tab: String) -> String? {
        let subscription = pools.compactMap(\.subscription).first
        let name = glance.spend?.plans.first { $0.id == subscription || $0.id == tab }?.name
        if let name, !name.isEmpty {
            for prefix in ["Claude ", "ChatGPT ", "OpenCode ", "GitHub Copilot "] where name.hasPrefix(prefix) {
                return String(name.dropFirst(prefix.count))
            }
            return name
        }
        if let pool = pools.first?.pool, pool.hasPrefix("github-copilot-"), let last = pool.split(separator: "-").last { return last.prefix(1).uppercased() + last.dropFirst() }
        return pools.first?.billing?.plan.map { $0.prefix(1).uppercased() + $0.dropFirst() }
    }

    func headerBadge(_ pools: [Pool], lines: [LimitLine]) -> HeaderBadge? {
        let states = pools.compactMap { $0.state?.uppercased() }
        if states.contains("EXHAUSTED") || lines.contains(where: \.full) { return HeaderBadge(word: "Used up", hot: true) }
        if states.contains(where: { ["CRITICAL", "LOW", "CONSERVE", "SCARCE"].contains($0) }) { return HeaderBadge(word: "Low", hot: true) }
        return nil
    }

    func incidentNote(_ pools: [Pool]) -> IncidentNote? {
        let words = ["maintenance": "Maintenance", "minor": "Minor issue", "major": "Major outage", "critical": "Major outage"]
        for pool in pools {
            guard let incident = pool.incident, let word = words[incident.level] else { continue }
            let text = incident.text ?? incident.affected.joined(separator: ", ")
            guard !text.isEmpty else { continue }
            return IncidentNote(word: word, text: text, severe: incident.level == "major" || incident.level == "critical")
        }
        return nil
    }

    func resetRows(_ pools: [Pool], tab: String, lines: [LimitLine]) -> [ResetRow] {
        var rows: [ResetRow] = []
        func win(_ key: String) -> String {
            if key == "weekly" { return "Weekly" }
            if key.hasPrefix("claude-weekly-scoped-") {
                let name = String(key.dropFirst("claude-weekly-scoped-".count))
                return name.prefix(1).uppercased() + name.dropFirst()
            }
            return key
        }
        for pool in pools {
            for (index, reset) in pool.limitResets.enumerated() where reset.left > 0 {
                let name: String
                if reset.clears.contains("weekly") { name = "Weekly" }
                else if let first = reset.clears.first { name = win(first) }
                else { name = "Limit" }
                let until = reset.expiresAt.flatMap(Self.controlDate).map(dayText)
                let effect = reset.clears.compactMap { clear -> String? in
                    guard let line = lines.first(where: { $0.id == "\(pool.pool):\(clear)" }), line.figure != "Not read" else { return nil }
                    return "\(win(clear)) \(Int(line.used.rounded()))% → 0%"
                }.joined(separator: " · ")
                let ask = "Use the " + (name == "Weekly" ? "weekly" : name) + " reset?"
                rows.append(ResetRow(id: reset.key ?? "\(pool.pool)-\(index)", name: reset.left > 1 ? "\(name) ×\(reset.left)" : name,
                                     next: reset.next, until: until, key: reset.key,
                                     canUse: tab == "claude" && pool.pool == "claude" && reset.key != nil && reset.usableNow,
                                     ask: ask, effect: effect.isEmpty ? nil : effect))
            }
        }
        return rows
    }

    func tokensModel(_ pools: [Pool]) -> TokensModel? {
        let series = pools.compactMap(\.tokens)
        guard !series.isEmpty else { return nil }
        let length = series.map { $0.days.count }.max() ?? 0
        var days = [Int64](repeating: 0, count: length)
        for s in series {
            for (offset, value) in s.days.reversed().enumerated() { days[length - 1 - offset] += value }
        }
        return TokensModel(today: Self.big(series.reduce(0) { $0 + $1.today }), days: days)
    }

    func creditsModel(_ pools: [Pool]) -> CreditsModel? {
        for pool in pools {
            if let credits = pool.credits, credits.unit != "limit resets", let balance = credits.balance {
                let amount = credits.unit == "USD" ? "$" + balance.formatted(.number.precision(.fractionLength(2)))
                    : balance.rounded(.down).formatted(.number.precision(.fractionLength(0))) + " " + (credits.unit ?? "credits")
                let date = (credits.renewsAt ?? credits.expiresAt).flatMap(Self.controlDate).map(dayText)
                return CreditsModel(amount: amount, until: date.map { (credits.renewsAt != nil ? "renews " : "until ") + $0 })
            }
            if let grant = pool.billing?.grants.first(where: { ($0.credits ?? 0) > 0 }), let left = grant.credits {
                return CreditsModel(amount: left.rounded(.down).formatted(.number.precision(.fractionLength(0))) + " credits",
                                    until: grant.expiresAt.flatMap(Self.controlDate).map { "until " + dayText($0) })
            }
        }
        return nil
    }

    static func statusURL(_ raw: String?) -> URL? {
        guard let raw, let parts = URLComponents(string: raw), parts.scheme == "https", let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil else { return nil }
        return parts.url
    }

    public func providerPage(for tab: String, show: PanelPrefs.Show) -> ProviderPage? {
        guard let group = groups.first(where: { $0.id == tab }) else { return nil }
        let pools = group.pools
        let agy = tab == "agy"
        var groups: [LimitGroup] = []
        if agy {
            let order = ["antigravity-gemini", "antigravity-3p", "antigravity-argon"]
            let byID = Dictionary(uniqueKeysWithValues: pools.map { ($0.pool, $0) })
            for id in order {
                guard let pool = byID[id] else { continue }
                var lines = limitLines(pool, show: show)
                var note: String?
                if id == "antigravity-argon" {
                    // Argon reports Gemini's own 5-hour window: say so, do not draw it twice.
                    let geminiNames = Set((byID["antigravity-gemini"]?.limits ?? []).compactMap(\.name))
                    let own = pool.limits.filter { $0.kind == "window" && $0.isActive }
                    lines = zip(own.sorted { ($0.minutes ?? 1e9) < ($1.minutes ?? 1e9) }, lines).filter { !geminiNames.contains($0.0.name ?? "") }.map(\.1)
                    if lines.isEmpty { note = "Shares the Gemini 5-hour limit" }
                }
                groups.append(LimitGroup(id: id, title: Self.agyTitles[id], note: note, lines: lines))
            }
        } else if tab == "other" {
            groups = []
        } else {
            let multi = pools.count > 1
            groups = pools.map { LimitGroup(id: $0.pool, title: multi ? $0.name : nil, note: nil, lines: limitLines($0, show: show)) }
        }
        let allLines = groups.flatMap(\.lines)
        let head = pools.first
        let policy: PolicyModel?
        if agy {
            policy = glance.subscriptions.first { $0.id == "antigravity" }.flatMap { subscription in
                let attribution = subscription.levelControl ?? pools.compactMap(\.levelControl)
                    .compactMap { record in Self.controlDate(record.when).map { ($0, record) } }.max { $0.0 < $1.0 }?.1
                return PolicyModel(id: subscription.id, rawLevel: subscription.level, attribution: attribution)
            }
        } else if pools.count == 1, tab != "other", let pool = head {
            policy = PolicyModel(id: pool.pool, rawLevel: pool.level, attribution: pool.levelControl)
        } else {
            policy = nil
        }
        let age = pools.compactMap { $0.staleAgeS }.max() ?? ageSeconds()
        let read = pools.contains { $0.measured || $0.usedPercent != nil }
        let title = agy ? "Antigravity" : (Self.titles[tab] ?? (pools.count > 1 ? tab : (head?.label ?? head?.name ?? tab)))
        let status = pools.compactMap { Self.statusURL($0.statusURL) }.first
        var links = [LinkItem(title: "Usage and cost", kind: .usage(agy ? "antigravity" : head?.pool))]
        if let status, tab != "other" { links.append(LinkItem(title: "Status", kind: .status(status))) }
        links.append(LinkItem(title: "Console", kind: .console))
        return ProviderPage(id: tab, title: title, plan: tab == "other" ? nil : planLabel(pools, tab: tab),
                            logo: tab == "other" ? nil : head.flatMap { ProviderLogo.forPool($0.pool) },
                            updated: tab == "other" ? "Not metered" : Self.updated(age: age, hasReading: read),
                            badge: headerBadge(pools, lines: allLines), incident: incidentNote(pools),
                            groups: groups, notMetered: tab == "other" ? pools.map { $0.name } : [],
                            resets: resetRows(pools, tab: tab, lines: allLines), tokens: tokensModel(pools),
                            policy: policy, credits: creditsModel(pools), links: links)
    }

    // MARK: Overview

    func tightestLine(_ pools: [Pool], tab: String, show: PanelPrefs.Show) -> (line: LimitLine, section: String?, runsOut: Double?)? {
        let live = pools.filter { !$0.isRetired }
        var best: (line: LimitLine, section: String?, runsOut: Double?, score: Double)?
        for pool in live.isEmpty ? pools : live {
            let windows = pool.limits.filter { $0.kind == "window" && $0.isActive && $0.usedPercent != nil }
            let single = windows.isEmpty && pool.limits.isEmpty && pool.measured && pool.usedPercent != nil
            let list: [Limit] = single ? [Limit(kind: "window", title: "Usage", usedPercent: pool.usedPercent, resets: pool.resets, resetAt: pool.resetAt, pace: pool.pace)] : windows
            for (index, limit) in list.enumerated() {
                let line = limitLine(limit, id: "\(pool.pool):\(limit.name ?? String(index))", show: show)
                let runs = (limit.pace?.status == "deficit" && !line.full) ? limit.pace?.runsOutInS : nil
                let score = line.full ? 1e12 : (runs.map { 1e9 - $0 } ?? line.used)
                if best == nil || score > best!.score { best = (line, Self.agyTitles[pool.pool], runs, score) }
            }
        }
        return best.map { ($0.line, $0.section, $0.runsOut) }
    }

    public func overviewPage(order: [String], show: PanelPrefs.Show) -> OverviewPage {
        let byID = Dictionary(uniqueKeysWithValues: groups.map { ($0.id, $0.pools) })
        let titles = Dictionary(uniqueKeysWithValues: tabs.map { ($0.id, $0) })
        var cards: [ProviderCard] = []
        var soonest: (words: String, seconds: Double)?
        for id in order {
            guard id != "other", let pools = byID[id], let tab = titles[id] else { continue }
            let tight = tightestLine(pools, tab: id, show: show)
            var line = tight?.line
            var window = line?.window ?? ""
            if let section = tight?.section, section != "Claude and GPT", var l = line {
                window = "\(section) \(l.window.lowercased())"
                l.window = window
                line = l
            } else if window.hasPrefix("Premium ") {
                let rest = String(window.dropFirst(8))
                window = rest.prefix(1).uppercased() + rest.dropFirst()
                line?.window = window
            }
            if let seconds = tight?.runsOut, soonest == nil || seconds < soonest!.seconds {
                soonest = ("\(tight?.section ?? tab.title) runs out in \(Self.span(seconds))", seconds)
            }
            let tickets = pools.flatMap(\.limitResets).filter { $0.left > 0 }.count
            cards.append(ProviderCard(id: id, title: tab.title, logo: tab.logo, tickets: tickets, line: line, window: window))
        }
        var alert: OverviewPage.Alert?
        if let soonest { alert = .init(text: soonest.words, hot: true) }
        else if cards.contains(where: { $0.line != nil }) { alert = .init(text: "Nothing runs out before it refills", hot: false) }
        return OverviewPage(alert: alert, providers: cards, subscriptions: subscriptionsModel(),
                            tokens: tokensModel(glance.pools),
                            links: [LinkItem(title: "Usage and cost", kind: .usage(nil)), LinkItem(title: "Console", kind: .console)])
    }

    // MARK: Subscriptions

    static func shareMark(_ fraction: String) -> String {
        let marks = ["1/2": "½", "1/3": "⅓", "2/3": "⅔", "1/4": "¼", "3/4": "¾"]
        if let mark = marks[fraction] { return mark }
        let parts = fraction.split(separator: "/").compactMap { Double($0) }
        return parts.count == 2 && parts[1] > 0 ? "\(Int((parts[0] / parts[1] * 100).rounded()))%" : fraction
    }

    static func money(_ amount: String?, _ currency: String?) -> String? {
        guard let amount, let value = Double(amount), value >= 0 else { return nil }
        let code = (currency ?? "").uppercased()
        let symbols = ["GBP": "£", "EUR": "€", "USD": "$"]
        let number = value == value.rounded() ? String(Int(value)) : String(format: "%.2f", value)
        return (symbols[code] ?? (code.isEmpty ? "" : code + " ")) + number
    }

    func subscriptionsModel() -> SubscriptionsModel? {
        guard let spend = glance.spend, !spend.unavailable, !spend.plans.isEmpty else { return nil }
        let rows = spend.plans.map { plan -> SubscriptionRow in
            let full = Self.money(plan.amount, plan.currency)
            let usd = plan.operatorUSD.flatMap(Double.init).map { "$\(Int($0.rounded()))" }
            let yours = usd ?? Self.money(plan.operatorAmount ?? plan.amount, plan.currency)
            return SubscriptionRow(id: plan.id, name: plan.name, share: plan.share.map(Self.shareMark),
                                   full: plan.share != nil ? full : nil, yours: yours ?? "No price", priced: yours != nil)
        }
        var total: String?
        if let own = spend.usd?.operatorAmount, let value = Double(own) {
            total = "≈ $\(Int(value.rounded()))"
        } else if spend.monthly.count == 1, let m = spend.monthly.first, let own = m.operator_, let text = Self.money(own, m.currency) {
            total = text
        }
        return SubscriptionsModel(rows: rows, total: total)
    }
}
