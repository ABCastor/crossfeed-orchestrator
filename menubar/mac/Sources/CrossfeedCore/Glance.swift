// Crossfeed menu bar: schema 1 of `fleetctl glance --json --no-refresh`.
//
// This is the app's only data source. There are no provider readers, cookies or network calls here.
import Foundation

public enum GlanceError: Error, Equatable {
    case unsupportedSchema(Int?)
    case malformed
}

public struct Pace: Decodable, Equatable {
    public var deltaPercent: Double?
    public var elapsedPercent: Double?
    public var usedPercent: Double?
    public var points: Int?
    public var status: String?   // on_pace | surplus | deficit
    public var early: Bool?
    public var runsOutInS: Double?
    public var runOutBasis: String?
    public var words: String?

    enum CodingKeys: String, CodingKey {
        case deltaPercent = "delta_percent", elapsedPercent = "elapsed_percent", usedPercent = "used_percent"
        case points, status, early, runsOutInS = "runs_out_in_s", runOutBasis = "run_out_basis", words
    }
}

public struct Limit: Decodable, Equatable {
    public var kind: String?
    public var name: String?
    public var title: String?
    public var scope: String?
    public var minutes: Double?
    public var usedPercent: Double?
    public var resets: String?
    public var resetAt: String?
    public var resetsInS: Double?
    public var resetUnknown: Bool?
    public var pace: Pace?
    public var state: String?
    /// False for a window that exists on the plan but is not running (Claude's 5-hour before the first message);
    /// then `note` says why. A missing key means active.
    public var active: Bool?
    public var note: String?
    /// Usable limit resets the account holds that clear this window (`resets_held`). Nil or 0 means none.
    public var resetsHeld: Int?

    enum CodingKeys: String, CodingKey {
        case kind, name, title, scope, minutes, usedPercent = "used_percent", resets, resetAt = "reset_at"
        case resetsInS = "resets_in_s", resetUnknown = "reset_unknown", pace, state, active, note
        case resetsHeld = "resets_held"
    }

    public var isActive: Bool { active != false }
    /// How many resets are held for this window; never negative.
    public var held: Int { max(0, resetsHeld ?? 0) }
}

public struct GlanceModel: Decodable, Equatable {
    public let id: String?
    public let name: String
    public let family: String?
    public let typicalCostUSD: Double?
    public let archived: Bool
    /// The day the maker withdraws the model (YYYY-MM-DD), when fleetctl says so.
    public let retiredOn: String?

    /// Archived, or withdrawn (even if the day is still ahead): never the allowance the bar shows.
    public var isRetired: Bool { archived || retiredOn != nil }

    enum CodingKeys: String, CodingKey {
        case name, model, family, typicalCostUSD = "typical_cost_usd", archived, retiredOn = "retired_on"
    }

    public init(from decoder: Decoder) throws {
        // Older schema-1 glance sends model names alone.
        if let name = try? decoder.singleValueContainer().decode(String.self) {
            self.name = name
            id = nil
            family = nil; typicalCostUSD = nil; archived = false; retiredOn = nil
            return
        }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .model)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? c.decode(String.self, forKey: .model)
        family = try c.decodeIfPresent(String.self, forKey: .family)
        typicalCostUSD = try c.decodeIfPresent(Double.self, forKey: .typicalCostUSD)
        archived = try c.decodeIfPresent(Bool.self, forKey: .archived) ?? false
        retiredOn = (try? c.decodeIfPresent(String.self, forKey: .retiredOn)).flatMap { $0 }.flatMap { $0.isEmpty ? nil : $0 }
    }
}

public struct GlanceSubscription: Decodable, Equatable {
    public let id: String
    public var level: String
    public var levelControl: PolicyAttribution?

    enum CodingKeys: String, CodingKey { case id, level, levelControl = "level_control" }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        level = try c.decode(String.self, forKey: .level)
        levelControl = (try? PolicyAttribution(from: decoder)) ?? (try? c.decode(PolicyAttribution.self, forKey: .levelControl))
    }
}

public struct Pool: Decodable, Equatable {
    public var pool: String
    public var subscription: String?
    public var name: String
    public var label: String?
    public var level: String?
    public var levelControl: PolicyAttribution?
    public var models: [GlanceModel]
    public var archivedModels: [GlanceModel]
    public var credits: Credits?
    public var statusURL: String?
    public var usageCost: UsageCost?
    public var perModel: [ModelSpend]
    public var incident: Incident?
    public var billing: Billing?
    public var modelQuota: [ModelQuota]
    public var state: String?
    public var measured: Bool
    public var full: Bool
    public var usedPercent: Double?
    public var resets: String?
    public var resetAt: String?
    public var pace: Pace?
    public var limits: [Limit]
    /// fleetctl sends null, a string, or an object with a `reason`; only the words survive.
    public var gaugeError: String?
    public var staleAgeS: Double?
    /// Today's tokens and the last 14 days, from the saved usage-cost caches. Nil when nothing was read.
    public var tokens: TokenSeries?
    /// Limit resets held: Claude's carry a `key` the menu can use.
    public var limitResets: [LimitReset]
    /// 100 × the resets held for the pool's tightest window (`reset_capacity_percent`); nil when none are held.
    public var resetCapacityPercent: Double?
    /// Spend, balance, models and daily activity of a pool billed per token, by prepaid credit or through a relay.
    public var api: ApiBlock?

    /// A pool whose every model is archived or withdrawn is on its way out: the bar never leads with it.
    public var isRetired: Bool {
        let all = models + archivedModels
        return !all.isEmpty && all.allSatisfy(\.isRetired)
    }

    enum CodingKeys: String, CodingKey {
        case pool, subscription, name, label, level, state, measured, full, usedPercent = "used_percent", resets
        case resetAt = "reset_at", pace, limits, gaugeError = "gauge_error", staleAgeS = "stale_age_s"
        case levelControl = "level_control", models, archivedModels = "archived"
        case credits, statusURL = "status_url", usageCost = "usage_cost", perModel = "per_model"
        case incident, billing, modelQuota = "model_quota", tokens, limitResets = "limit_resets", api
        case resetCapacityPercent = "reset_capacity_percent"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        pool = try c.decode(String.self, forKey: .pool)
        subscription = try? c.decodeIfPresent(String.self, forKey: .subscription)
        name = (try? c.decode(String.self, forKey: .name)) ?? pool
        label = try? c.decodeIfPresent(String.self, forKey: .label)
        level = try? c.decodeIfPresent(String.self, forKey: .level)
        levelControl = (try? PolicyAttribution(from: decoder)) ?? (try? c.decode(PolicyAttribution.self, forKey: .levelControl))
        models = (try? c.decode([GlanceModel].self, forKey: .models)) ?? []
        archivedModels = (try? c.decode([GlanceModel].self, forKey: .archivedModels)) ?? []
        credits = try? c.decode(Credits.self, forKey: .credits)
        statusURL = try? c.decode(String.self, forKey: .statusURL)
        usageCost = try? c.decode(UsageCost.self, forKey: .usageCost)
        perModel = (try? c.decode([ModelSpend].self, forKey: .perModel)) ?? []
        incident = try? c.decodeIfPresent(Incident.self, forKey: .incident)
        billing = try? c.decodeIfPresent(Billing.self, forKey: .billing)
        modelQuota = (try? c.decode([ModelQuota].self, forKey: .modelQuota)) ?? []
        state = try? c.decodeIfPresent(String.self, forKey: .state)
        measured = (try? c.decode(Bool.self, forKey: .measured)) ?? false
        full = (try? c.decode(Bool.self, forKey: .full)) ?? false
        usedPercent = try? c.decodeIfPresent(Double.self, forKey: .usedPercent)
        resets = try? c.decodeIfPresent(String.self, forKey: .resets)
        resetAt = try? c.decodeIfPresent(String.self, forKey: .resetAt)
        pace = try? c.decodeIfPresent(Pace.self, forKey: .pace)
        limits = (try? c.decodeIfPresent([Limit].self, forKey: .limits)) ?? []
        staleAgeS = try? c.decodeIfPresent(Double.self, forKey: .staleAgeS)
        tokens = try? c.decodeIfPresent(TokenSeries.self, forKey: .tokens)
        limitResets = (try? c.decodeIfPresent([LimitReset].self, forKey: .limitResets)) ?? []
        api = try? c.decodeIfPresent(ApiBlock.self, forKey: .api)
        resetCapacityPercent = try? c.decodeIfPresent(Double.self, forKey: .resetCapacityPercent)
        if let text = try? c.decodeIfPresent(String.self, forKey: .gaugeError) {
            gaugeError = text
        } else if let object = try? c.decodeIfPresent([String: JSONScalar].self, forKey: .gaugeError) {
            gaugeError = object["reason"]?.text ?? "no reading"
        } else if (try? c.decodeIfPresent(Bool.self, forKey: .gaugeError)) == true {
            gaugeError = "no reading"
        } else {
            gaugeError = nil
        }
    }
}

/// Just enough of a JSON value to read a `reason` out of an error object.
struct JSONScalar: Decodable {
    var text: String?
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        text = try? c.decode(String.self)
    }
}

public struct Money: Decodable, Equatable {
    public var currency: String
    public var total: String
    public var operator_: String?
    enum CodingKeys: String, CodingKey { case currency, total, operator_ = "operator" }
}

/// One subscription's price, when the glance lists plans: "Claude Max 20x", 183, GBP, month. Every field but the
/// name may be missing; `source` is "default" (from the plan) or "operator" (typed in the console).
public struct PlanPrice: Decodable, Equatable {
    public var id: String
    public var name: String
    public var amount: String?
    public var currency: String?
    public var period: String?
    public var source: String?
    /// The operator's part of a split as a reduced fraction ("1/2"), nil when the plan is not split.
    public var share: String?
    /// What the operator pays a month, in the plan's own currency.
    public var operatorAmount: String?
    /// The same in USD at the glance's reference rates, so rows add up to the USD total.
    public var operatorUSD: String?

    enum CodingKeys: String, CodingKey { case id, name, amount, currency, period, source, share, operatorAmount = "operator_amount", operatorUSD = "operator_usd" }

    public init(id: String, name: String = "", amount: String? = nil, currency: String? = nil, period: String? = nil, source: String? = nil,
                share: String? = nil, operatorAmount: String? = nil) {
        self.id = id; self.name = name; self.amount = amount; self.currency = currency; self.period = period; self.source = source
        self.share = share; self.operatorAmount = operatorAmount
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? c.decode(String.self, forKey: .id)) ?? ""
        name = (try? c.decode(String.self, forKey: .name)) ?? id
        // The amount arrives as a string ("183.00") or a number; either reads the same.
        amount = (try? c.decodeIfPresent(String.self, forKey: .amount))
            ?? (try? c.decodeIfPresent(Double.self, forKey: .amount)).map { String($0) }
        currency = try? c.decodeIfPresent(String.self, forKey: .currency)
        period = try? c.decodeIfPresent(String.self, forKey: .period)
        source = try? c.decodeIfPresent(String.self, forKey: .source)
        share = try? c.decodeIfPresent(String.self, forKey: .share)
        operatorAmount = (try? c.decodeIfPresent(String.self, forKey: .operatorAmount))
            ?? (try? c.decodeIfPresent(Double.self, forKey: .operatorAmount)).map { String($0) }
        operatorUSD = (try? c.decodeIfPresent(String.self, forKey: .operatorUSD))
            ?? (try? c.decodeIfPresent(Double.self, forKey: .operatorUSD)).map { String($0) }
    }
}

public struct Spend: Decodable, Equatable {
    public var plans: [PlanPrice]
    public var monthly: [Money]
    public var unpriced: Int
    public var unsplit: Int
    public var unavailable: Bool
    /// One USD total at reference rates, and the operator's own part of it. Nil when no rate is known.
    public var usd: UsdTotal?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        usd = try? c.decodeIfPresent(UsdTotal.self, forKey: .usd)
        plans = (try? c.decode([PlanPrice].self, forKey: .plans)) ?? (try? c.decode([PlanPrice].self, forKey: .subscriptions)) ?? []
        monthly = (try? c.decode([Money].self, forKey: .monthly)) ?? []
        unpriced = (try? c.decode(Int.self, forKey: .unpriced)) ?? 0
        unsplit = (try? c.decode(Int.self, forKey: .unsplit)) ?? 0
        unavailable = (try? c.decode(Bool.self, forKey: .unavailable)) ?? false
    }
    enum CodingKeys: String, CodingKey { case plans, subscriptions, monthly, unpriced, unsplit, unavailable, usd }
}

public struct Readings: Decodable, Equatable {
    public var measuredPools: Int?
    public var newestAgeS: Double?
    enum CodingKeys: String, CodingKey { case measuredPools = "measured_pools", newestAgeS = "newest_age_s" }
}

public struct Glance: Decodable, Equatable {
    public var schema: Int
    public var title: String
    public var pools: [Pool]
    public var subscriptions: [GlanceSubscription]
    public var readings: Readings?
    /// Nil when the key is missing (an older fleetctl): the menu then says spend is unavailable.
    public var spend: Spend?
    /// Tiles the enabled plugins drew, already validated by fleetctl and checked again here.
    public var plugins: [PluginEntry]

    enum CodingKeys: String, CodingKey { case schema, title, pools, subscriptions, readings, spend = "subscription_spend", plugins }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schema = try c.decode(Int.self, forKey: .schema)
        guard schema == 1 else { throw GlanceError.unsupportedSchema(schema) }
        title = (try? c.decode(String.self, forKey: .title)) ?? ""
        pools = try c.decode([Pool].self, forKey: .pools)
        subscriptions = (try? c.decode([GlanceSubscription].self, forKey: .subscriptions)) ?? []
        readings = try? c.decodeIfPresent(Readings.self, forKey: .readings)
        spend = try? c.decodeIfPresent(Spend.self, forKey: .spend)
        plugins = (try? c.decodeIfPresent([PluginEntry].self, forKey: .plugins)) ?? []
    }

    public func policyLevel(for id: String) -> String? {
        subscriptions.first { $0.id == id }?.level ?? pools.first { $0.pool == id }?.level
    }

    /// The command committed, but its readback failed. Keep the confirmed level without inventing an actor.
    public mutating func applyConfirmed(_ change: PolicyChange) {
        let subscription = subscriptions.firstIndex { $0.id == change.pool }
        if let subscription {
            subscriptions[subscription].level = change.level.rawValue
            subscriptions[subscription].levelControl = nil
        }
        for index in pools.indices where pools[index].pool == change.pool
            || (subscription != nil && pools[index].subscription == change.pool) {
            pools[index].level = change.level.rawValue
            pools[index].levelControl = nil
        }
    }

    /// Decodes fleetctl's stdout. Any other schema, or a body that is not glance JSON, is refused.
    public static func decode(_ data: Data) throws -> Glance {
        do {
            return try JSONDecoder().decode(Glance.self, from: data)
        } catch let error as GlanceError {
            throw error
        } catch {
            if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let schema = object["schema"] as? Int, schema != 1 {
                throw GlanceError.unsupportedSchema(schema)
            }
            throw GlanceError.malformed
        }
    }
}
