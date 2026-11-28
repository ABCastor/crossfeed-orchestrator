// Crossfeed menu: the `api` block a pool carries when it is billed per token, by prepaid credit or through a relay
// (OpenRouter, Gemini API, Crossfeed Chat). Every field is optional and every decoder is tolerant: a part that is
// missing or malformed is simply absent, never a failure.
import Foundation

public struct ApiSpend: Equatable {
    public var todayUSD: Double?
    public var weekUSD: Double?
    public var monthUSD: Double?
}

public struct ApiDay: Equatable {
    public var date: String
    public var usd: Double?
    public var requests: Int64?
    public var tokens: Int64?
}

public struct ApiModelSpend: Equatable {
    public var model: String
    public var usd: Double?
    public var requests: Int64?
}

public struct ApiBlock: Decodable, Equatable {
    public var balanceUSD: Double?
    public var creditsUSD: Double?
    public var keyLimitUSD: Double?
    public var keyRemainingUSD: Double?
    public var spend: ApiSpend?
    /// The budget the reading carries (`source` says whose it is). The operator's own, from panel.json, wins.
    public var budgetUSD: Double?
    public var budgetSource: String?
    /// Up to 30 days, oldest first.
    public var days: [ApiDay]
    public var topModels: [ApiModelSpend]
    public var source: String?
    public var observedAt: String?
    /// A billing page, when the reading names one (https only).
    public var billingURL: URL?

    enum CodingKeys: String, CodingKey {
        case balance = "balance_usd", credits = "credits_usd", keyLimit = "key_limit_usd", keyRemaining = "key_remaining_usd"
        case spend, budget, days, topModels = "top_models", source, observedAt = "observed_at", billingURL = "billing_url"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func number(_ key: CodingKeys) -> Double? {
            guard let v = try? c.decodeIfPresent(Double.self, forKey: key), v.isFinite else { return nil }
            return v
        }
        balanceUSD = number(.balance)
        creditsUSD = number(.credits)
        keyLimitUSD = number(.keyLimit)
        keyRemainingUSD = number(.keyRemaining)
        if let s = try? c.nestedContainer(keyedBy: SpendKeys.self, forKey: .spend) {
            func part(_ key: SpendKeys) -> Double? { (try? s.decodeIfPresent(Double.self, forKey: key)).flatMap { $0 }.flatMap { $0.isFinite ? $0 : nil } }
            let value = ApiSpend(todayUSD: part(.today), weekUSD: part(.week), monthUSD: part(.month))
            spend = value == ApiSpend() ? nil : value
        }
        if let b = try? c.nestedContainer(keyedBy: BudgetKeys.self, forKey: .budget) {
            if let v = try? b.decodeIfPresent(Double.self, forKey: .monthly), v.isFinite, v > 0 { budgetUSD = v }
            budgetSource = (try? b.decodeIfPresent(String.self, forKey: .source)).flatMap { $0 }
        }
        let dayItems = (try? c.decode([Lossy<DayItem>].self, forKey: .days)) ?? []
        self.days = Array(dayItems.compactMap(\.value).map(\.day).suffix(30))
        let modelItems = (try? c.decode([Lossy<ModelItem>].self, forKey: .topModels)) ?? []
        topModels = modelItems.compactMap(\.value).compactMap(\.spend)
        source = (try? c.decodeIfPresent(String.self, forKey: .source)).flatMap { $0 }
        observedAt = (try? c.decodeIfPresent(String.self, forKey: .observedAt)).flatMap { $0 }
        billingURL = (try? c.decodeIfPresent(String.self, forKey: .billingURL)).flatMap { $0 }.flatMap(Presenter.statusURL)
    }

    private enum SpendKeys: String, CodingKey { case today = "today_usd", week = "week_usd", month = "month_usd" }
    private enum BudgetKeys: String, CodingKey { case monthly = "monthly_usd", source }

    /// One element that may be malformed: it decodes to nil instead of failing the list.
    private struct Lossy<T: Decodable>: Decodable {
        let value: T?
        init(from decoder: Decoder) throws { value = try? T(from: decoder) }
    }

    private struct DayItem: Decodable {
        var day: ApiDay
        enum Keys: String, CodingKey { case date, usd, requests, tokens }
        init(from decoder: Decoder) throws {
            let d = try decoder.container(keyedBy: Keys.self)
            day = ApiDay(date: (try? d.decode(String.self, forKey: .date)) ?? "",
                         usd: (try? d.decodeIfPresent(Double.self, forKey: .usd)).flatMap { $0 },
                         requests: (try? d.decodeIfPresent(Int64.self, forKey: .requests)).flatMap { $0 },
                         tokens: (try? d.decodeIfPresent(Int64.self, forKey: .tokens)).flatMap { $0 })
        }
    }

    private struct ModelItem: Decodable {
        var spend: ApiModelSpend?
        enum Keys: String, CodingKey { case model, usd, requests }
        init(from decoder: Decoder) throws {
            let m = try decoder.container(keyedBy: Keys.self)
            if let name = try? m.decode(String.self, forKey: .model), !name.isEmpty {
                spend = ApiModelSpend(model: name, usd: (try? m.decodeIfPresent(Double.self, forKey: .usd)).flatMap { $0 },
                                      requests: (try? m.decodeIfPresent(Int64.self, forKey: .requests)).flatMap { $0 })
            }
        }
    }

    /// Where the figures come from, in the words the tile says.
    public var sourceNote: String? {
        switch source {
        case "openrouter-api": return "OpenRouter account"
        case "admin-cost-api": return "Provider cost API"
        case "local-meter": return "Measured by Crossfeed"
        default: return nil
        }
    }
}
