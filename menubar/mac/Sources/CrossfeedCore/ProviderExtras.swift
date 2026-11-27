import Foundation

public struct Credits: Decodable, Equatable {
    public let balance: Double?
    public let unit: String?
    public let kind: String?
    public let expiresAt: String?
    public let renewsAt: String?
    public let source: String?

    enum CodingKeys: String, CodingKey {
        case balance, unit, kind, source, expiresAt = "expires_at", renewsAt = "renews_at"
    }
}

public struct CostModel: Decodable, Equatable {
    public let model: String
    public let usd: Double?
    public let tokens: Int64?
}

public struct UsageCost: Decodable, Equatable {
    public let todayUSD: Double?
    public let last30DaysUSD: Double?
    public let todayTokens: Int64?
    public let last30DaysTokens: Int64?
    public let source: String?
    /// Desktop-history cost by model (Antigravity); absent for providers that report none.
    public let byModel: [CostModel]

    enum CodingKeys: String, CodingKey {
        case todayUSD = "today_usd", last30DaysUSD = "last_30d_usd", todayTokens = "today_tokens"
        case last30DaysTokens = "last_30d_tokens", source, byModel = "by_model"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        todayUSD = try? c.decodeIfPresent(Double.self, forKey: .todayUSD)
        last30DaysUSD = try? c.decodeIfPresent(Double.self, forKey: .last30DaysUSD)
        todayTokens = try? c.decodeIfPresent(Int64.self, forKey: .todayTokens)
        last30DaysTokens = try? c.decodeIfPresent(Int64.self, forKey: .last30DaysTokens)
        source = try? c.decodeIfPresent(String.self, forKey: .source)
        byModel = (try? c.decode([CostModel].self, forKey: .byModel)) ?? []
    }
}

/// A provider incident from its public status feed (statuspage JSON, Google Workspace incidents, ...).
public struct Incident: Decodable, Equatable {
    public let level: String
    public let text: String?
    public let affected: [String]
    public let since: String?

    enum CodingKeys: String, CodingKey { case level, text, affected, since }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        level = try c.decode(String.self, forKey: .level)
        text = try? c.decodeIfPresent(String.self, forKey: .text)
        affected = (try? c.decode([String].self, forKey: .affected)) ?? []
        since = try? c.decodeIfPresent(String.self, forKey: .since)
    }
}

public struct BillingGrant: Decodable, Equatable {
    public let credits: Double?
    public let granted: Double?
    public let expiresAt: String?
    enum CodingKeys: String, CodingKey { case credits, granted, expiresAt = "expires_at" }
}

/// Codex's paid period and credit history, read with the Codex CLI's own sign-in.
public struct Billing: Decodable, Equatable {
    public let plan: String?
    public let period: String?
    public let activeUntil: String?
    public let renewsAt: String?
    public let willRenew: Bool?
    public let grants: [BillingGrant]
    public let creditsToday: Double?
    public let credits7d: Double?
    public let credits30d: Double?

    enum CodingKeys: String, CodingKey {
        case plan, period, grants, activeUntil = "active_until", renewsAt = "renews_at", willRenew = "will_renew"
        case creditsToday = "credits_today", credits7d = "credits_7d", credits30d = "credits_30d"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        plan = try? c.decodeIfPresent(String.self, forKey: .plan)
        period = try? c.decodeIfPresent(String.self, forKey: .period)
        activeUntil = try? c.decodeIfPresent(String.self, forKey: .activeUntil)
        renewsAt = try? c.decodeIfPresent(String.self, forKey: .renewsAt)
        willRenew = try? c.decodeIfPresent(Bool.self, forKey: .willRenew)
        grants = (try? c.decode([BillingGrant].self, forKey: .grants)) ?? []
        creditsToday = try? c.decodeIfPresent(Double.self, forKey: .creditsToday)
        credits7d = try? c.decodeIfPresent(Double.self, forKey: .credits7d)
        credits30d = try? c.decodeIfPresent(Double.self, forKey: .credits30d)
    }
}

/// Per-model quota from the provider (Argon models share the Gemini 5-hour pool).
public struct ModelQuota: Decodable, Equatable {
    public let label: String
    public let usedPercent: Double?
    public let resetAt: String?
    public let sharedWith: String?
    enum CodingKeys: String, CodingKey { case label, usedPercent = "used_percent", resetAt = "reset_at", sharedWith = "shared_with" }
}

public struct ModelSpend: Decodable, Equatable {
    public let model: String
    public let usd30Days: Double?
    public let requests: Int64?

    enum CodingKeys: String, CodingKey { case model, usd30Days = "usd_30d", requests }
}

public struct ExtraRow: Equatable {
    public let label: String
    public let detail: String
}

public struct IncidentLine: Equatable {
    public let level: String
    public let title: String
    public let detail: String
    public var isSevere: Bool { level == "major" || level == "critical" }
}

public struct ProviderExtras: Equatable {
    public let creditTitle: String
    public let credits: [String]
    /// The same credits as one line each, the date on the right: "Extra usage left · US$12.54" / "Renews 6 Dec".
    public let creditRows: [ExtraRow]
    public let usage: [ExtraRow]
    public let perModel: [ExtraRow]
    public let status: URL?
    /// Nil unless the provider's status feed reports something other than all clear.
    public let incident: IncidentLine?
    public let billing: [String]
    public let modelQuota: [ExtraRow]
    public let costByModel: [ExtraRow]

    private static let incidentTitles = ["maintenance": "Maintenance", "minor": "Degraded", "major": "Major incident", "critical": "Outage"]

    init(pool: Pool) { self.init(pool: pool, showIncident: true) }

    init(pool: Pool, showIncident: Bool) {
        let date = Date.FormatStyle().month(.abbreviated).day().year()
        if let credits = pool.credits {
            if credits.kind == "extra_usage" { creditTitle = "Extra usage remaining" }
            else if credits.unit == "limit resets" { creditTitle = "Reset credits" }
            else { creditTitle = "Credits" }
            let balance = credits.balance.map { value in
                if credits.unit == "USD" { return Self.usd(value) }
                let number = value.formatted(.number.precision(.fractionLength(0...2)))
                guard let unit = credits.unit, !unit.isEmpty else { return number }
                return "\(number) \(unit)"
            }
            let expires = credits.expiresAt.flatMap(Presenter.controlDate).map { "Expires on \($0.formatted(date))" }
            let renews = credits.renewsAt.flatMap(Presenter.controlDate).map { "New credits on \($0.formatted(date))" }
            self.credits = [balance, expires, renews].compactMap { $0 }
            let day = Date.FormatStyle().day().month(.abbreviated)
            let renewDay = credits.renewsAt.flatMap(Presenter.controlDate).map { "Renews " + $0.formatted(day) }
            let expiryDay = credits.expiresAt.flatMap(Presenter.controlDate).map { "Expires " + $0.formatted(day) }
            let label = creditTitle.replacingOccurrences(of: " remaining", with: " left") + (balance.map { " · " + $0 } ?? "")
            creditRows = [ExtraRow(label: label, detail: renewDay ?? expiryDay ?? "")]
                + (renewDay != nil ? [expiryDay].compactMap { $0 }.map { ExtraRow(label: "", detail: $0) } : [])
        } else {
            creditTitle = "Credits"
            credits = []
            creditRows = []
        }
        let cost = pool.usageCost
        usage = [Self.row("Today", cost?.todayUSD, cost?.todayTokens, "tokens"),
                 Self.row("Last 30 days", cost?.last30DaysUSD, cost?.last30DaysTokens, "tokens")].compactMap { $0 }
        costByModel = (cost?.byModel ?? []).compactMap { Self.row($0.model, $0.usd, $0.tokens, "tokens") }
        if showIncident, let incident = pool.incident, let title = Self.incidentTitles[incident.level] {
            let when = incident.since.flatMap(Presenter.controlDate).map { "since \($0.formatted(Date.FormatStyle().month(.abbreviated).day().hour().minute()))" }
            let parts = [incident.text, incident.affected.isEmpty ? nil : incident.affected.joined(separator: ", "), when]
            self.incident = IncidentLine(level: incident.level, title: title,
                                         detail: parts.compactMap { $0 }.joined(separator: " · "))
        } else {
            incident = nil
        }
        var lines: [String] = []
        if let billing = pool.billing {
            let plan = [billing.plan, billing.period].compactMap { $0?.capitalized }.joined(separator: " · ")
            if let renews = billing.renewsAt.flatMap(Presenter.controlDate) {
                lines.append("\(plan.isEmpty ? "Subscription" : plan) renews \(renews.formatted(date))")
            } else if let until = billing.activeUntil.flatMap(Presenter.controlDate) {
                lines.append("\(plan.isEmpty ? "Subscription" : plan) ends \(until.formatted(date))" + (billing.willRenew == false ? ", not renewing" : ""))
            }
            for grant in billing.grants {
                guard let left = grant.credits else { continue }
                let of = grant.granted.map { " of \($0.formatted(.number.precision(.fractionLength(0))))" } ?? ""
                let expires = grant.expiresAt.flatMap(Presenter.controlDate).map { ", expire \($0.formatted(date))" } ?? ""
                lines.append("Funded credits \(left.formatted(.number.precision(.fractionLength(0))))\(of) left\(expires)")
            }
            // Credits used by day, week and month are a usage report; the console has it.
        }
        billing = lines
        modelQuota = pool.modelQuota.compactMap { quota in
            guard let used = quota.usedPercent else { return nil }
            let reset = quota.resetAt.flatMap(Presenter.controlDate).map { "resets \($0.formatted(Date.FormatStyle().month(.abbreviated).day().hour().minute()))" }
            let shared = quota.sharedWith.map { "shares \($0)" }
            return ExtraRow(label: quota.label,
                            detail: ([used.formatted(.number.precision(.fractionLength(0))) + "% used"] + [reset, shared].compactMap { $0 }).joined(separator: " · "))
        }
        let hidden = Set((pool.archivedModels + pool.models.filter(\.archived)).flatMap { [$0.id ?? $0.name, $0.name] })
        perModel = pool.perModel.filter { !hidden.contains($0.model) }.sorted {
            let lhs = $0.usd30Days ?? -.infinity, rhs = $1.usd30Days ?? -.infinity
            return lhs != rhs ? lhs > rhs : $0.model < $1.model
        }.compactMap { Self.row($0.model, $0.usd30Days, $0.requests, $0.requests == 1 ? "request" : "requests") }
        // Antigravity's allowances share one status feed: its link is said once, with its incident.
        if showIncident, let raw = pool.statusURL, let parts = URLComponents(string: raw), parts.scheme == "https",
           let host = parts.host, !host.isEmpty, parts.user == nil, parts.password == nil {
            status = parts.url
        } else {
            status = nil
        }
    }

    private static func usd(_ value: Double) -> String {
        value.formatted(.currency(code: "USD").precision(.fractionLength(2...4)))
    }

    private static func row(_ label: String, _ usd: Double?, _ count: Int64?, _ unit: String) -> ExtraRow? {
        let fields = [usd.map(Self.usd), count.map { "\($0.formatted()) \(unit)" }].compactMap { $0 }
        guard !fields.isEmpty else { return nil }
        return ExtraRow(label: label, detail: fields.joined(separator: " · "))
    }
}
