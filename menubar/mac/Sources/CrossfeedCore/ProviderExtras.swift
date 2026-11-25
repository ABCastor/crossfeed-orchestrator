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

public struct UsageCost: Decodable, Equatable {
    public let todayUSD: Double?
    public let last30DaysUSD: Double?
    public let todayTokens: Int64?
    public let last30DaysTokens: Int64?
    public let source: String?

    enum CodingKeys: String, CodingKey {
        case todayUSD = "today_usd", last30DaysUSD = "last_30d_usd", todayTokens = "today_tokens"
        case last30DaysTokens = "last_30d_tokens", source
    }
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

public struct ProviderExtras: Equatable {
    public let creditTitle: String
    public let credits: [String]
    public let usage: [ExtraRow]
    public let perModel: [ExtraRow]
    public let status: URL?

    init(pool: Pool) {
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
        } else {
            creditTitle = "Credits"
            credits = []
        }
        let cost = pool.usageCost
        usage = [Self.row("Today", cost?.todayUSD, cost?.todayTokens, "tokens"),
                 Self.row("Last 30 days", cost?.last30DaysUSD, cost?.last30DaysTokens, "tokens")].compactMap { $0 }
        let hidden = Set((pool.archivedModels + pool.models.filter(\.archived)).flatMap { [$0.id ?? $0.name, $0.name] })
        perModel = pool.perModel.filter { !hidden.contains($0.model) }.sorted {
            let lhs = $0.usd30Days ?? -.infinity, rhs = $1.usd30Days ?? -.infinity
            return lhs != rhs ? lhs > rhs : $0.model < $1.model
        }.compactMap { Self.row($0.model, $0.usd30Days, $0.requests, $0.requests == 1 ? "request" : "requests") }
        if let raw = pool.statusURL, let parts = URLComponents(string: raw), parts.scheme == "https",
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
