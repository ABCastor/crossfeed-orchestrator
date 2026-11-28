// Crossfeed menu: what an API tab draws (OpenRouter, Gemini API, Crossfeed Chat), the Status tile, and the charts whose
// metric and range the operator picks. Pure functions of (Glance, now, panel.json), like the rest of the tiles.
import Foundation

public struct ApiSpendModel: Equatable {
    /// What the bar measures: this month against a budget, a key's limit, or a prepaid credit.
    public enum Basis: Equatable { case budget, keyLimit, credit, none }
    public var basis: Basis
    public var figure: String            // "$6.61": this month's spend
    public var caption: String           // "this month"
    public var detail: String?           // "of $20 budget", "$13.39 left of $20"
    public var bar: PaceBarModel?
    public var pace: PaceWord?
    public var runsOut: String?
    public var today: String?
    public var week: String?
    public var month: String?
    public var hot: Bool
    public var used: Double?             // percent of the basis
    public var expected: Double?         // percent of the month gone, with a budget
    public var source: String?           // "Measured by Crossfeed"
    public var read: Bool                // false when the pool reported no `api` block
    /// The overview card's line: the same bar, the figure the card prints on the right.
    public var card: LimitLine?

    var tabBar: BarModel? {
        guard let used else { return nil }
        let kind: PaceKind = pace.map { $0.kind == .deficit ? .deficit : ($0.kind == .surplus ? .surplus : .onPace) } ?? .unknown
        return BarModel(used: used, expected: expected, pace: kind, hot: hot)
    }
}

public struct ApiBalanceRow: Equatable, Identifiable {
    public var id: String
    public var label: String             // "Credit", "Key limit"
    public var figure: String            // "$1.79"
    public var detail: String            // "left of $26"
    public var bar: PaceBarModel?
    public var hot: Bool
}

public struct ApiBalanceModel: Equatable {
    public var rows: [ApiBalanceRow]
}

public struct ApiModelRow: Equatable, Identifiable {
    public var id: String
    public var name: String              // "claude-sonnet-5.5"
    public var vendor: String?           // "anthropic"
    public var usd: String               // "$3.10"
    public var requests: String?         // "221 requests"
    public var share: Double             // 0...1 of the top model's spend
}

public struct StatusModel: Equatable {
    public enum Level: Equatable { case clear, notice, severe, unknown }
    public var level: Level
    public var word: String              // "No incidents", "Minor issue", "Status not reported"
    public var text: String?
    public var url: URL?
}

public struct IncidentRow: Equatable, Identifiable {
    public var id: String                // the provider's tab
    public var title: String
    public var logo: ProviderLogo?
    public var word: String
    public var text: String
    public var severe: Bool
    public var url: URL?
}

extension Presenter {
    // MARK: Money

    public static func usd(_ value: Double) -> String {
        let v = max(0, value)
        if v >= 1000 { return "$" + v.formatted(.number.precision(.fractionLength(0))) }
        return v >= 100 ? String(format: "$%.0f", v) : String(format: "$%.2f", v)
    }

    /// A budget or limit the operator typed: whole dollars when it is whole.
    public static func usdPlain(_ value: Double) -> String {
        value == value.rounded() ? "$" + Int(value).formatted() : String(format: "$%.2f", value)
    }

    /// True for a tab that holds one API pool (OpenRouter, Gemini API, Crossfeed Chat).
    public func isAPITab(_ id: String) -> Bool {
        guard let pools = groups.first(where: { $0.id == id })?.pools, pools.count == 1, let pool = pools.first else { return false }
        return Self.isAPI(pool)
    }

    /// True when the pool reports money (a spend figure, or a cost per day): a budget has something to be measured against.
    public func isBudgetable(_ id: String) -> Bool {
        guard isAPITab(id), let api = groups.first(where: { $0.id == id })?.pools.first?.api else { return false }
        return api.spend != nil || api.days.contains { $0.usd != nil }
    }

    /// The budget the reading itself carries for a tab's pool (set somewhere else, such as the console), used until the operator picks one here.
    public func readingBudget(_ id: String) -> Double? {
        groups.first(where: { $0.id == id })?.pools.first?.api?.budgetUSD
    }

    // MARK: Spend

    /// Where the month stands, in percent of its length.
    func monthElapsed() -> Double {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        guard let month = calendar.dateInterval(of: .month, for: now), month.duration > 0 else { return 0 }
        return min(100, max(0, now.timeIntervalSince(month.start) / month.duration * 100))
    }

    func monthSpendUSD(_ api: ApiBlock) -> Double? {
        if let month = api.spend?.monthUSD { return month }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let c = calendar.dateComponents([.year, .month], from: now)
        guard let y = c.year, let m = c.month else { return nil }
        let prefix = String(format: "%04d-%02d", y, m)
        let days = api.days.filter { $0.date.hasPrefix(prefix) }.compactMap(\.usd)
        return days.isEmpty ? nil : days.reduce(0, +)
    }

    /// The monthly budget the operator set for this pool, else the one the reading carries.
    public func budget(for pool: Pool) -> Double? { panel.budgets[pool.pool] ?? pool.api?.budgetUSD }

    func paceWord(used: Double, elapsed: Double) -> PaceWord {
        let delta = used - elapsed
        return abs(delta) < 1 ? PaceWord(text: "on pace", kind: .onPace)
            : PaceWord(text: "\(Int(abs(delta).rounded()))% \(delta > 0 ? "deficit" : "surplus")", kind: delta > 0 ? .deficit : .surplus)
    }

    public func apiSpend(_ pool: Pool, show: PanelPrefs.Show = .used) -> ApiSpendModel? {
        guard let api = pool.api else { return nil }
        if let runs = runsOnly(api) { return runs }
        let month = monthSpendUSD(api)
        let budget = budget(for: pool)
        var model = ApiSpendModel(basis: .none, figure: month.map(Self.usd) ?? "Not read", caption: month == nil ? "no spend reported" : "this month",
                                  detail: nil, bar: nil, pace: nil, runsOut: nil, today: api.spend?.todayUSD.map(Self.usd),
                                  week: api.spend?.weekUSD.map(Self.usd), month: month.map(Self.usd), hot: false, used: nil,
                                  expected: nil, source: api.sourceNote, read: true, card: nil)
        let left = show == .left
        if let budget, let month {
            let used = month / budget * 100
            let elapsed = monthElapsed()
            let full = used >= Self.usedUp
            model.basis = .budget
            model.used = min(100, max(0, used))
            model.expected = elapsed
            model.bar = PaceBarModel.make(used: min(100, used), elapsed: full ? nil : elapsed, show: show, full: full)
            model.pace = full ? nil : paceWord(used: used, elapsed: elapsed)
            let remaining = budget - month
            model.detail = full ? "over the \(Self.usdPlain(budget)) budget"
                : (left ? "\(Self.usd(remaining)) left of \(Self.usdPlain(budget))" : "of \(Self.usdPlain(budget)) budget")
            if !full, used > elapsed, month > 0, elapsed > 0 {
                var calendar = Calendar(identifier: .gregorian)
                calendar.timeZone = timeZone
                if let span = calendar.dateInterval(of: .month, for: now) {
                    let gone = now.timeIntervalSince(span.start)
                    let seconds = remaining / (month / gone)
                    if seconds > 0, seconds < span.end.timeIntervalSince(now) { model.runsOut = "Runs out in " + Self.span(seconds) }
                }
            }
            model.hot = full || model.runsOut != nil
        } else if let limit = api.keyLimitUSD, limit > 0, let remaining = api.keyRemainingUSD {
            let used = (limit - remaining) / limit * 100
            let full = used >= Self.usedUp
            model.basis = .keyLimit
            model.used = min(100, max(0, used))
            model.bar = PaceBarModel.make(used: min(100, max(0, used)), elapsed: nil, show: show, full: full)
            model.detail = left ? "key limit \(Self.usd(remaining)) left of \(Self.usdPlain(limit))" : "of \(Self.usdPlain(limit)) key limit"
            model.hot = full || used >= 90
        } else if let credits = api.creditsUSD, credits > 0, let balance = api.balanceUSD {
            let used = (credits - balance) / credits * 100
            let full = used >= Self.usedUp
            model.basis = .credit
            model.used = min(100, max(0, used))
            model.bar = PaceBarModel.make(used: min(100, max(0, used)), elapsed: nil, show: show, full: full)
            model.detail = left ? "\(Self.usd(balance)) left of \(Self.usdPlain(credits)) credit" : "of \(Self.usdPlain(credits)) credit"
            model.hot = full || used >= 90
        }
        model.card = apiCardLine(model, pool: pool, show: show, budget: budget, api: api, month: month)
        return model
    }

    /// A relay that reports run counts and no money: the tile counts runs, with no bar.
    private func runsOnly(_ api: ApiBlock) -> ApiSpendModel? {
        guard api.spend == nil, !api.days.contains(where: { $0.usd != nil }), api.days.contains(where: { $0.requests != nil }) else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let c = calendar.dateComponents([.year, .month], from: now)
        let prefix = String(format: "%04d-%02d", c.year ?? 0, c.month ?? 0)
        let counts = api.days.map { ($0.date, $0.requests ?? 0) }
        let month = counts.filter { $0.0.hasPrefix(prefix) }.reduce(0) { $0 + $1.1 }
        let week = counts.suffix(7).reduce(0) { $0 + $1.1 }
        let today = counts.last?.1
        func runs(_ n: Int64) -> String { "\(n) run" + (n == 1 ? "" : "s") }
        var model = ApiSpendModel(basis: .none, figure: runs(month), caption: "this month", detail: "no cost reported", bar: nil, pace: nil,
                                  runsOut: nil, today: today.map(runs), week: runs(Int64(week)), month: runs(month), hot: false, used: nil,
                                  expected: nil, source: api.sourceNote, read: true, card: nil)
        model.card = LimitLine(id: "spend", figure: runs(month), hot: false, window: "This month", resets: "no cost reported",
                               bar: PaceBarModel(segments: [], tick: nil), pace: nil, runsOut: nil, used: 0, full: false)
        return model
    }

    /// The Overview card: the figure on the right, the basis named under the bar.
    private func apiCardLine(_ model: ApiSpendModel, pool: Pool, show: PanelPrefs.Show, budget: Double?, api: ApiBlock, month: Double?) -> LimitLine? {
        let left = show == .left
        let figure: String, window: String, resets: String
        switch model.basis {
        case .budget:
            let remaining = max(0, (budget ?? 0) - (month ?? 0))
            figure = model.used.map { $0 >= Self.usedUp } == true ? "Over budget" : (left ? Self.usd(remaining) + " left" : Self.usd(month ?? 0) + " spent")
            window = "This month"; resets = Self.usdPlain(budget ?? 0) + " budget"
        case .keyLimit:
            let limit = api.keyLimitUSD ?? 0, remaining = api.keyRemainingUSD ?? 0
            figure = left ? Self.usd(remaining) + " left" : Self.usd(limit - remaining) + " spent"
            window = "Key limit"; resets = Self.usdPlain(limit)
        case .credit:
            let credits = api.creditsUSD ?? 0, balance = api.balanceUSD ?? 0
            figure = left ? Self.usd(balance) + " left" : Self.usd(credits - balance) + " spent"
            window = "Credit"; resets = Self.usdPlain(credits)
        case .none:
            figure = model.read && month != nil ? Self.usd(month ?? 0) : "Not read"
            window = "This month"; resets = month == nil ? "no spend reported" : "no budget"
        }
        return LimitLine(id: pool.pool + ":spend", figure: figure, hot: model.hot, window: window, resets: resets,
                         bar: model.bar ?? PaceBarModel(segments: [], tick: nil), pace: model.pace, runsOut: model.runsOut,
                         used: model.used ?? 0, full: (model.used ?? 0) >= Self.usedUp)
    }

    // MARK: Balance, models

    func apiBalance(_ pool: Pool, show: PanelPrefs.Show) -> ApiBalanceModel? {
        guard let api = pool.api else { return nil }
        var rows: [ApiBalanceRow] = []
        if let balance = api.balanceUSD {
            var bar: PaceBarModel?
            var hot = false
            if let credits = api.creditsUSD, credits > 0 {
                let used = min(100, max(0, (credits - balance) / credits * 100))
                bar = PaceBarModel.make(used: used, elapsed: nil, show: show, full: used >= Self.usedUp)
                hot = used >= 90
            }
            rows.append(ApiBalanceRow(id: "credit", label: "Credit", figure: Self.usd(balance),
                                      detail: api.creditsUSD.map { "left of \(Self.usdPlain($0))" } ?? "left", bar: bar, hot: hot))
        }
        if let limit = api.keyLimitUSD, limit > 0 {
            let remaining = api.keyRemainingUSD
            var bar: PaceBarModel?
            var hot = false
            if let remaining {
                let used = min(100, max(0, (limit - remaining) / limit * 100))
                bar = PaceBarModel.make(used: used, elapsed: nil, show: show, full: used >= Self.usedUp)
                hot = used >= 90
            }
            rows.append(ApiBalanceRow(id: "key", label: "Key limit", figure: remaining.map(Self.usd) ?? Self.usdPlain(limit),
                                      detail: remaining == nil ? "per key" : "left of \(Self.usdPlain(limit))", bar: bar, hot: hot))
        }
        return rows.isEmpty ? nil : ApiBalanceModel(rows: rows)
    }

    /// The top models by spend; a relay that reports only run counts lists them by runs.
    func apiModels(_ pool: Pool) -> [ApiModelRow] {
        guard let api = pool.api else { return [] }
        let hasMoney = api.topModels.contains { $0.usd != nil }
        func weight(_ m: ApiModelSpend) -> Double? { hasMoney ? m.usd : m.requests.map(Double.init) }
        let top = api.topModels.compactMap { m in weight(m).map { (m, $0) } }.sorted { $0.1 > $1.1 }.prefix(4)
        let peak = top.first?.1 ?? 0
        return top.map { m, value in
            let parts = m.model.split(separator: "/", maxSplits: 1).map(String.init)
            let runs = m.requests.map { "\($0) request" + ($0 == 1 ? "" : "s") }
            return ApiModelRow(id: m.model, name: parts.count == 2 ? parts[1] : m.model, vendor: parts.count == 2 ? parts[0] : nil,
                               usd: hasMoney ? Self.usd(value) : (m.requests.map { "\($0) runs" } ?? "–"),
                               requests: hasMoney ? runs : nil, share: peak > 0 ? value / peak : 0)
        }
    }

    // MARK: Status

    static let incidentWords = ["maintenance": "Maintenance", "minor": "Minor issue", "major": "Major outage", "critical": "Major outage"]

    func statusModel(_ pools: [Pool]) -> StatusModel {
        let url = pools.compactMap { Self.statusURL($0.statusURL) }.first
        for pool in pools {
            guard let incident = pool.incident, let word = Self.incidentWords[incident.level] else { continue }
            let text = incident.text ?? incident.affected.joined(separator: ", ")
            return StatusModel(level: incident.level == "major" || incident.level == "critical" ? .severe : .notice, word: word,
                               text: text.isEmpty ? nil : text, url: url)
        }
        if pools.contains(where: { $0.incident != nil }) { return StatusModel(level: .clear, word: "No incidents", text: nil, url: url) }
        return StatusModel(level: .unknown, word: "Status not reported", text: nil, url: url)
    }

    /// The incidents the providers report now, one row per provider, in the tab order given.
    public func incidents(order: [String]) -> [IncidentRow] {
        let byID = Dictionary(uniqueKeysWithValues: groups.map { ($0.id, $0.pools) })
        let titles = Dictionary(uniqueKeysWithValues: tabs.map { ($0.id, $0) })
        return order.compactMap { id in
            guard let pools = byID[id], let tab = titles[id], let note = incidentNote(pools) else { return nil }
            return IncidentRow(id: id, title: tab.title, logo: tab.logo, word: note.word, text: note.text, severe: note.severe,
                               url: pools.compactMap { Self.statusURL($0.statusURL) }.first)
        }
    }

    // MARK: Charts

    private func series(_ pools: [Pool], _ metric: ChartMetric) -> (values: [Double], today: Double?)? {
        switch metric {
        case .tokens:
            let own = pools.compactMap(\.tokens)
            if !own.isEmpty {
                let length = own.map { $0.days.count }.max() ?? 0
                var days = [Double](repeating: 0, count: length)
                for s in own { for (offset, value) in s.days.reversed().enumerated() { days[length - 1 - offset] += Double(value) } }
                return (days, Double(own.reduce(0) { $0 + $1.today }))
            }
            let api = pools.compactMap(\.api).filter { $0.days.contains { $0.tokens != nil } }
            return merged(api) { $0.tokens.map(Double.init) }.map { ($0, $0.last) }
        case .cost:
            let api = pools.compactMap(\.api).filter { $0.days.contains { $0.usd != nil } }
            let todays = api.compactMap { $0.spend?.todayUSD }
            return merged(api) { $0.usd }.map { values in (values, todays.isEmpty ? values.last : todays.reduce(0, +)) }
        case .requests:
            let api = pools.compactMap(\.api).filter { $0.days.contains { $0.requests != nil } }
            return merged(api) { $0.requests.map(Double.init) }.map { ($0, $0.last) }
        }
    }

    private func merged(_ blocks: [ApiBlock], _ value: (ApiDay) -> Double?) -> [Double]? {
        guard !blocks.isEmpty else { return nil }
        let length = blocks.map { $0.days.count }.max() ?? 0
        var out = [Double](repeating: 0, count: length)
        for block in blocks { for (offset, day) in block.days.reversed().enumerated() { out[length - 1 - offset] += value(day) ?? 0 } }
        return out
    }

    /// The chart a tile draws: the metric and range the operator picked (`tile_options`), else tokens over 14 days
    /// (or what the pool has).
    public func chart(tab: String, tile: String, pools: [Pool]) -> TokensModel? {
        let all = ChartMetric.allCases.filter { series(pools, $0) != nil }
        // Tokens for a subscription; for an API tab what it is billed in, else the run count.
        let preferred: [ChartMetric] = tile == TileID.activity ? [.cost, .requests, .tokens] : [.tokens, .requests, .cost]
        guard let first = preferred.first(where: all.contains) else { return nil }
        let option = panel.option(tab, tile)
        let metric = option.metric.flatMap { all.contains($0) ? $0 : nil } ?? first
        let window = option.days ?? 14
        guard let data = series(pools, metric) else { return nil }
        let shown = Array(data.values.suffix(window))
        let today = data.today ?? shown.last ?? 0
        let figure: String, caption: String, days: [Int64]
        switch metric {
        case .tokens: figure = Self.big(Int64(today)); caption = " tokens today"; days = shown.map { Int64($0) }
        case .cost: figure = Self.usd(today); caption = " spent today"; days = shown.map { Int64(($0 * 100).rounded()) }
        case .requests: figure = Self.big(Int64(today)); caption = " requests today"; days = shown.map { Int64($0) }
        }
        return TokensModel(today: figure, days: days, metric: metric, window: window, available: all, caption: caption)
    }
}
