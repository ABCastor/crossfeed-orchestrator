// Crossfeed menu: the round 11 parts of the glance: tokens, held limit resets, the USD total and plugin tiles.
// Every decoder here is tolerant: a part that is missing or malformed is simply absent, never a failure.
import Foundation

/// Today's tokens and the last 14 daily totals, oldest first.
public struct TokenSeries: Decodable, Equatable {
    public var today: Int64
    public var days: [Int64]

    public init(today: Int64, days: [Int64]) { self.today = today; self.days = days }

    enum CodingKeys: String, CodingKey { case today, days }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        today = max(0, (try? c.decode(Int64.self, forKey: .today)) ?? 0)
        days = ((try? c.decode([Int64].self, forKey: .days)) ?? []).suffix(30).map { max(0, $0) }
    }
}

/// A limit reset the account holds. Claude's carry `key`, the handle `fleetctl use-reset claude --key` takes.
public struct LimitReset: Decodable, Equatable {
    public var label: String
    public var left: Int
    public var total: Int?
    public var clears: [String]
    public var expiresAt: String?
    public var usableNow: Bool
    public var next: Bool
    public var key: String?
    public var kind: String?

    enum CodingKeys: String, CodingKey { case label, left, total, clears, expiresAt = "expires_at", usableNow = "usable_now", next, key, kind }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        label = (try? c.decode(String.self, forKey: .label)) ?? "Limit reset"
        left = (try? c.decode(Int.self, forKey: .left)) ?? 1
        total = try? c.decodeIfPresent(Int.self, forKey: .total)
        clears = (try? c.decode([String].self, forKey: .clears)) ?? []
        expiresAt = try? c.decodeIfPresent(String.self, forKey: .expiresAt)
        usableNow = (try? c.decode(Bool.self, forKey: .usableNow)) ?? true
        next = (try? c.decode(Bool.self, forKey: .next)) ?? false
        key = try? c.decodeIfPresent(String.self, forKey: .key)
        kind = try? c.decodeIfPresent(String.self, forKey: .kind)
    }
}

public struct UsdTotal: Decodable, Equatable {
    public var total: String?
    public var operatorAmount: String?
    public var ratesAsOf: String?
    public var approximate: Bool

    enum CodingKeys: String, CodingKey { case total, operatorAmount = "operator", ratesAsOf = "rates_as_of", approximate }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func text(_ key: CodingKeys) -> String? {
            (try? c.decodeIfPresent(String.self, forKey: key)) ?? (try? c.decodeIfPresent(Double.self, forKey: key)).map { String($0) }
        }
        total = text(.total)
        operatorAmount = text(.operatorAmount)
        ratesAsOf = try? c.decodeIfPresent(String.self, forKey: .ratesAsOf)
        approximate = (try? c.decode(Bool.self, forKey: .approximate)) ?? true
    }
}

// MARK: Plugin tiles (docs/PLUGINS.md in the contract: declarative, no code runs in the menu)

public struct PluginRow: Equatable {
    public enum Kind: Equatable {
        case bar(used: Double, pace: Double?, window: String?, resetsAt: String?)
        case value(value: String, tone: Tone)
        case sparkline(values: [Double], today: String?)
        case text
        case link(url: URL)
    }
    public enum Tone: String, Equatable { case none, ok, warn, error }
    public var label: String
    public var kind: Kind
}

public struct PluginTileDoc: Equatable {
    public enum Status: String, Equatable { case ok, warn, error }
    public var title: String
    public var status: Status
    public var rows: [PluginRow]

    public static let invalid = PluginTileDoc(title: "", status: .error, rows: [PluginRow(label: "Plugin output invalid", kind: .text)])
    public static let maxRows = 8, maxText = 120

    /// The same checks fleetctl made, once more: at most 8 rows, text up to 120 characters, finite numbers,
    /// used and pace 0 to 100, https links only, unknown kinds dropped. Anything else is the invalid tile.
    static func validated(_ value: JSONValue?, fallbackTitle: String) -> PluginTileDoc {
        func invalid() -> PluginTileDoc { PluginTileDoc(title: fallbackTitle, status: .error, rows: invalid_rows) }
        guard let object = value?.objectValue else { return invalid() }
        func text(_ v: JSONValue?) -> String? {
            guard let s = v?.stringValue, s.count <= maxText else { return nil }
            return s
        }
        func number(_ v: JSONValue?) -> Double? {
            guard let n = v?.numberValue, n.isFinite else { return nil }
            return n
        }
        let status = PluginTileDoc.Status(rawValue: object["status"]?.stringValue ?? "ok") ?? .ok
        guard let rawRows = object["rows"]?.arrayValue ?? Optional([]), rawRows.count <= maxRows else { return invalid() }
        var rows: [PluginRow] = []
        for raw in rawRows {
            guard let r = raw.objectValue, let kind = r["kind"]?.stringValue else { continue }
            switch kind {
            case "bar":
                guard let label = text(r["label"]), let used = number(r["used"]), (0...100).contains(used) else { return invalid() }
                let pace = number(r["pace"])
                if let pace, !(0...100).contains(pace) { return invalid() }
                rows.append(PluginRow(label: label, kind: .bar(used: used, pace: pace, window: text(r["window"]), resetsAt: text(r["resets_at"]))))
            case "value":
                guard let label = text(r["label"]), let value = text(r["value"]) else { return invalid() }
                rows.append(PluginRow(label: label, kind: .value(value: value, tone: PluginRow.Tone(rawValue: r["tone"]?.stringValue ?? "") ?? .none)))
            case "sparkline":
                guard let label = text(r["label"]), let values = r["values"]?.arrayValue, values.count <= 60 else { return invalid() }
                let numbers = values.compactMap { number($0) }
                guard numbers.count == values.count else { return invalid() }
                rows.append(PluginRow(label: label, kind: .sparkline(values: numbers, today: text(r["today"]))))
            case "text":
                guard let body = text(r["text"]) else { return invalid() }
                rows.append(PluginRow(label: body, kind: .text))
            case "link":
                guard let label = text(r["label"]), let raw = text(r["url"]), let url = URL(string: raw), url.scheme == "https",
                      url.host?.isEmpty == false, url.user == nil, url.password == nil else { return invalid() }
                rows.append(PluginRow(label: label, kind: .link(url: url)))
            default: continue
            }
        }
        return PluginTileDoc(title: text(object["title"]) ?? fallbackTitle, status: status, rows: rows)
    }

    private static let invalid_rows = [PluginRow(label: "Plugin output invalid", kind: .text)]
}

/// One tile a plugin drew, as the glance carries it.
public struct PluginEntry: Decodable, Equatable, Identifiable {
    public var plugin: String
    public var tile: String
    public var tab: String
    public var doc: PluginTileDoc
    public var observedAt: String?
    public var error: String?

    /// The id the panel file uses for this tile: `plugin:<id>/<tile>`.
    public var id: String { "plugin:\(plugin)/\(tile)" }

    enum CodingKeys: String, CodingKey { case plugin, tile, tab, doc, observedAt = "observed_at", error }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        plugin = try c.decode(String.self, forKey: .plugin)
        tile = try c.decode(String.self, forKey: .tile)
        tab = PanelIDs.menu((try? c.decode(String.self, forKey: .tab)) ?? "overview")
        observedAt = try? c.decodeIfPresent(String.self, forKey: .observedAt)
        let failure = (try? c.decodeIfPresent(String.self, forKey: .error)).flatMap { $0 }
        error = failure
        let raw = try? c.decodeIfPresent(JSONValue.self, forKey: .doc)
        let fallback = tile
        if failure != nil || raw == nil || raw == JSONValue.null {
            doc = PluginTileDoc(title: fallback, status: .error, rows: [PluginRow(label: failure.map { String($0.prefix(PluginTileDoc.maxText)) } ?? "No reading", kind: .text)])
        } else {
            doc = PluginTileDoc.validated(raw, fallbackTitle: fallback)
        }
    }
}

// MARK: Using a Claude reset

/// What `fleetctl use-reset claude --key <key> --yes --json` printed, in the few words the menu shows.
public enum ResetOutcome: Equatable {
    case used(left: Int)
    case failed(String)

    public static func parse(_ data: Data, exitCode: Int32) -> ResetOutcome {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return .failed(exitCode == 0 ? "No answer" : "Not used")
        }
        if object["ok"] as? Bool == true {
            return .used(left: (object["left"] as? Int) ?? 0)
        }
        let reason = (object["error"] as? String).map { Presenter.sentence($0, limit: 90) } ?? "Not used"
        return .failed(reason)
    }
}
