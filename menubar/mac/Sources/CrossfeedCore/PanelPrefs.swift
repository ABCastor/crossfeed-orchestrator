// Crossfeed menu: panel.json, the one preferences file the Mac menu and the web panel share.
//
//   ~/.config/orchestrator/panel.json   (XDG_CONFIG_HOME respected; FLEET_PANEL_PREFS overrides)
//   {"schema":1, "show":"used", "tabs":[...], "hidden_tabs":[...], "tiles":{tab:[ids]}, "hidden_tiles":{tab:[ids]}}
//
// Mode 600, atomic replace, unknown keys kept as they are. In memory the tabs use the menu's own ids
// (agy, go); the file uses the contract's (antigravity, opencode). Foundation only.
import Foundation

/// Just enough JSON to keep what this version does not know about.
public enum JSONValue: Equatable, Decodable {
    case null, bool(Bool), number(Double), string(String), array([JSONValue]), object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else if let v = try? c.decode([String: JSONValue].self) { self = .object(v) }
        else { throw DecodingError.dataCorruptedError(in: c, debugDescription: "not JSON") }
    }

    init(any: Any) {
        switch any {
        case let v as NSNumber where CFGetTypeID(v) == CFBooleanGetTypeID(): self = .bool(v.boolValue)
        case let v as NSNumber: self = .number(v.doubleValue)
        case let v as String: self = .string(v)
        case let v as [Any]: self = .array(v.map(JSONValue.init(any:)))
        case let v as [String: Any]: self = .object(v.mapValues(JSONValue.init(any:)))
        default: self = .null
        }
    }

    var any: Any {
        switch self {
        case .null: return NSNull()
        case .bool(let v): return v
        case .number(let v): return v == v.rounded() && abs(v) < 1e15 ? Int(v) : v
        case .string(let v): return v
        case .array(let v): return v.map(\.any)
        case .object(let v): return v.mapValues(\.any)
        }
    }

    public var stringValue: String? { if case .string(let v) = self { return v }; return nil }
    public var numberValue: Double? { if case .number(let v) = self { return v }; return nil }
    public var arrayValue: [JSONValue]? { if case .array(let v) = self { return v }; return nil }
    public var objectValue: [String: JSONValue]? { if case .object(let v) = self { return v }; return nil }
}

/// The ids the panel file uses for the tabs whose id differs from the menu's own.
public enum PanelIDs {
    static let toFile = ["agy": "antigravity", "go": "opencode"]
    static let fromFile = ["antigravity": "agy", "opencode": "go"]
    public static func file(_ id: String) -> String { toFile[id] ?? id }
    public static func menu(_ id: String) -> String { fromFile[id] ?? id }
}

public struct PanelPrefs: Equatable {
    public enum Show: String, CaseIterable { case used, left }

    public static let schema = 1
    public static let fixedFirst = "overview"
    public static let fixedLast = "other"
    public static let defaultTabs = ["overview", "claude", "codex", "copilot", "agy", "go", "other"]
    /// What every provider tab can hold, in the default order. Plugin tiles are added by the operator.
    public static let providerTiles = ["limits", "resets", "tokens", "policy", "credits", "links"]
    public static let overviewTiles = ["alert", "providers", "subscriptions", "tokens", "links"]
    /// Tiles that sit two to a row when next to each other.
    public static let halfTiles: Set<String> = ["tokens", "policy", "credits"]

    public var show = Show.used
    public var tabs: [String] = PanelPrefs.defaultTabs
    public var hiddenTabs: [String] = []
    public var tiles: [String: [String]] = [:]
    public var hiddenTiles: [String: [String]] = [:]
    /// Keys this version does not know, written back untouched.
    public var extra: [String: JSONValue] = [:]

    public init() {}

    public static func defaultTiles(for tab: String) -> [String] {
        tab == "overview" ? overviewTiles : (tab == "other" ? ["limits"] : providerTiles)
    }

    // MARK: Tabs

    /// The provider tabs in the saved order, over what is there now. Overview is not in the list (it is always first),
    /// and "other" always goes last.
    public func orderedTabs(_ available: [String]) -> [String] {
        let present = Set(available)
        var seen = Set<String>()
        let ids = (tabs + available).filter { $0 != PanelPrefs.fixedFirst && present.contains($0) && seen.insert($0).inserted }
        return ids.filter { $0 != PanelPrefs.fixedLast } + ids.filter { $0 == PanelPrefs.fixedLast }
    }

    public func isTabVisible(_ id: String) -> Bool { id == PanelPrefs.fixedFirst || !hiddenTabs.contains(id) }

    public mutating func toggleTab(_ id: String) {
        guard id != PanelPrefs.fixedFirst else { return }
        if let i = hiddenTabs.firstIndex(of: id) { hiddenTabs.remove(at: i) } else { hiddenTabs.append(id) }
    }

    /// Moves `source` to `target`'s place among `available`, keeping the slots of tabs that are absent now.
    @discardableResult
    public mutating func moveTab(_ source: String, onto target: String, among available: [String]) -> Bool {
        var visible = orderedTabs(available)
        guard source != target, source != PanelPrefs.fixedLast, target != PanelPrefs.fixedLast,
              let from = visible.firstIndex(of: source), let to = visible.firstIndex(of: target) else { return false }
        visible.remove(at: from)
        visible.insert(source, at: to)
        let present = Set(available)
        var remaining = visible.makeIterator()
        var seen = Set<String>()
        let all = (tabs + available).filter { $0 != PanelPrefs.fixedFirst && seen.insert($0).inserted }
        tabs = [PanelPrefs.fixedFirst] + all.map { present.contains($0) ? (remaining.next() ?? $0) : $0 }
        return true
    }

    // MARK: Tiles

    /// The tiles of a tab to draw, in order: the saved order first (what is not there now drops out), then built-ins that
    /// are new to the list. Hidden tiles drop out. A plugin tile shows only where the operator put it.
    public func tileOrder(_ tab: String, available: [String]) -> [String] {
        let present = Set(available), hidden = Set(hiddenTiles[tab] ?? [])
        var seen = Set<String>()
        let saved = tiles[tab] ?? PanelPrefs.defaultTiles(for: tab)
        let newBuiltIns = PanelPrefs.defaultTiles(for: tab).filter { !(tiles[tab] ?? []).contains($0) }
        return (saved + (tiles[tab] == nil ? [] : newBuiltIns)).filter {
            present.contains($0) && !hidden.contains($0) && seen.insert($0).inserted
        }
    }

    /// Every tile the settings list shows for a tab: what is on, then what is off.
    public func listed(_ tab: String, builtIns: [String]) -> (on: [String], off: [String]) {
        let hidden = Set(hiddenTiles[tab] ?? [])
        var seen = Set<String>()
        let saved = tiles[tab] ?? PanelPrefs.defaultTiles(for: tab)
        let order = (saved + PanelPrefs.defaultTiles(for: tab)).filter { id in
            (builtIns.contains(id) || id.hasPrefix("plugin:")) && seen.insert(id).inserted
        }
        return (order.filter { !hidden.contains($0) }, order.filter { hidden.contains($0) })
    }

    /// Saves the order of the tiles shown on a tab, keeping the slots of tiles that are not drawn now.
    public mutating func setTileOrder(_ tab: String, shown: [String]) {
        let shownSet = Set(shown)
        let rest = (tiles[tab] ?? PanelPrefs.defaultTiles(for: tab)).filter { !shownSet.contains($0) }
        tiles[tab] = shown + rest
    }

    public mutating func setTile(_ tab: String, _ id: String, on: Bool) {
        var hidden = hiddenTiles[tab] ?? []
        if on {
            hidden.removeAll { $0 == id }
            var order = tiles[tab] ?? PanelPrefs.defaultTiles(for: tab)
            if !order.contains(id) { order.append(id) }
            tiles[tab] = order
        } else if id.hasPrefix("plugin:") {
            tiles[tab] = (tiles[tab] ?? PanelPrefs.defaultTiles(for: tab)).filter { $0 != id }
        } else if !hidden.contains(id) {
            hidden.append(id)
        }
        hiddenTiles[tab] = hidden.isEmpty ? nil : hidden
    }

    /// Overview draws "providers" as one tile per provider. A drag gives a flat order of `provider:<id>` and the
    /// other tiles; the providers' own order becomes the tab order, and the group's place in the list is kept.
    public mutating func setOverviewOrder(_ flat: [String], providers: [String]) {
        let ids = flat.compactMap { $0.hasPrefix("provider:") ? String($0.dropFirst(9)) : nil }
        let others = flat.map { $0.hasPrefix("provider:") ? "providers" : $0 }
        var seen = Set<String>()
        let order = others.filter { seen.insert($0).inserted }
        setTileOrder("overview", shown: order)
        if !ids.isEmpty {
            var visible = orderedTabs(providers)
            var queue = ids.makeIterator()
            visible = visible.map { ids.contains($0) ? (queue.next() ?? $0) : $0 }
            let present = Set(providers)
            let kept = tabs.filter { $0 != PanelPrefs.fixedFirst && !present.contains($0) }
            tabs = [PanelPrefs.fixedFirst] + visible + kept
        }
    }

    // MARK: File form

    init(file object: [String: Any]) {
        let raw = object.mapValues(JSONValue.init(any:))
        func strings(_ value: JSONValue?) -> [String] {
            (value?.arrayValue ?? []).compactMap(\.stringValue).filter { !$0.isEmpty && $0.count <= 80 }
        }
        func lists(_ value: JSONValue?) -> [String: [String]] {
            var out: [String: [String]] = [:]
            for (key, list) in value?.objectValue ?? [:] where key.count <= 80 {
                out[PanelIDs.menu(key)] = Array(strings(list).prefix(64))
            }
            return out
        }
        if raw["show"]?.stringValue == "left" { show = .left }
        let saved = strings(raw["tabs"]).map(PanelIDs.menu)
        var seen = Set<String>()
        tabs = (saved + PanelPrefs.defaultTabs).filter { seen.insert($0).inserted }
        hiddenTabs = strings(raw["hidden_tabs"]).map(PanelIDs.menu).filter { $0 != PanelPrefs.fixedFirst }
        tiles = lists(raw["tiles"])
        hiddenTiles = lists(raw["hidden_tiles"]).filter { !$0.value.isEmpty }
        let known: Set<String> = ["schema", "show", "tabs", "hidden_tabs", "tiles", "hidden_tiles"]
        extra = raw.filter { !known.contains($0.key) }
    }

    /// The known keys, written over `base` (what the file holds now) so keys another version added survive.
    func file(over base: [String: Any]) -> [String: Any] {
        var out = base
        for (key, value) in extra { out[key] = value.any }
        out["schema"] = PanelPrefs.schema
        out["show"] = show.rawValue
        out["tabs"] = tabs.map(PanelIDs.file)
        out["hidden_tabs"] = hiddenTabs.map(PanelIDs.file)
        out["tiles"] = Dictionary(uniqueKeysWithValues: tiles.map { (PanelIDs.file($0.key), $0.value) })
        out["hidden_tiles"] = Dictionary(uniqueKeysWithValues: hiddenTiles.map { (PanelIDs.file($0.key), $0.value) })
        return out
    }
}

public struct PanelPrefsStore {
    public let url: URL

    public init(url: URL) { self.url = url }

    /// FLEET_PANEL_PREFS, else $XDG_CONFIG_HOME/orchestrator/panel.json, else ~/.config/orchestrator/panel.json.
    public static func defaultURL(environment: [String: String] = ProcessInfo.processInfo.environment,
                                  home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        if let path = environment["FLEET_PANEL_PREFS"], path.hasPrefix("/") { return URL(fileURLWithPath: path) }
        let config = environment["XDG_CONFIG_HOME"].flatMap { $0.hasPrefix("/") ? URL(fileURLWithPath: $0) : nil }
            ?? home.appendingPathComponent(".config")
        return config.appendingPathComponent("orchestrator").appendingPathComponent("panel.json")
    }

    private func rawFile() -> [String: Any]? {
        guard let data = try? Data(contentsOf: url), data.count <= 1 << 20,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return object
    }

    public var exists: Bool { FileManager.default.fileExists(atPath: url.path) }

    /// Defaults filled; a missing or unreadable file reads as the defaults.
    public func load() -> PanelPrefs { PanelPrefs(file: rawFile() ?? [:]) }

    /// Writes the whole document to a temporary file in the same folder, then replaces the file (mode 600).
    @discardableResult
    public func save(_ prefs: PanelPrefs) -> Bool {
        let object = prefs.file(over: rawFile() ?? [:])
        guard JSONSerialization.isValidJSONObject(object),
              let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]) else { return false }
        let folder = url.deletingLastPathComponent()
        let fm = FileManager.default
        do {
            try fm.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let temp = folder.appendingPathComponent(".panel-\(UUID().uuidString).tmp")
            guard fm.createFile(atPath: temp.path, contents: data, attributes: [.posixPermissions: 0o600]) else { return false }
            if rename(temp.path, url.path) != 0 {
                try? fm.removeItem(at: temp)
                return false
            }
            return true
        } catch {
            return false
        }
    }
}

/// The tab order, the hidden tabs and the Claude block order used to live in UserDefaults. They move into the file once.
public enum PanelMigration {
    static let marker = "crossfeed.panel.migrated"
    static let legacyKeys = ["crossfeed.provider-order", "crossfeed.tabs.hidden", "crossfeed.tabs.hiddenInOverview", "sectionOrder.claude"]

    /// The Claude card's old block ids, as the new tiles: every window row is the limits tile.
    static func claudeTiles(_ old: [String]) -> [String] {
        var seen = Set<String>()
        let mapped = old.compactMap { id -> String? in
            if id.hasPrefix("row:") { return "limits" }
            return id == "policy" ? "policy" : nil
        }.filter { seen.insert($0).inserted }
        return mapped + PanelPrefs.providerTiles.filter { !mapped.contains($0) }
    }

    /// Returns true when it moved something. Runs once per preferences domain; a file that already says a thing wins.
    @discardableResult
    public static func run(store: PanelPrefsStore, defaults: UserDefaults) -> Bool {
        guard !defaults.bool(forKey: marker) else { return false }
        let order = defaults.stringArray(forKey: "crossfeed.provider-order") ?? []
        let hidden = defaults.stringArray(forKey: "crossfeed.tabs.hidden") ?? []
        let blocks = defaults.stringArray(forKey: "sectionOrder.claude") ?? []
        let hasLegacy = !order.isEmpty || !hidden.isEmpty || !blocks.isEmpty
        if hasLegacy {
            var prefs = store.load()
            let raw = store.exists
            if !order.isEmpty && !(raw && prefs.tabs != PanelPrefs.defaultTabs) {
                var seen = Set<String>()
                prefs.tabs = ([PanelPrefs.fixedFirst] + order + PanelPrefs.defaultTabs).filter { seen.insert($0).inserted }
            }
            if !hidden.isEmpty && prefs.hiddenTabs.isEmpty { prefs.hiddenTabs = hidden.filter { $0 != PanelPrefs.fixedFirst } }
            if !blocks.isEmpty && prefs.tiles["claude"] == nil { prefs.tiles["claude"] = claudeTiles(blocks) }
            guard store.save(prefs) else { return false }
        }
        defaults.set(true, forKey: marker)
        for key in legacyKeys { defaults.removeObject(forKey: key) }
        return hasLegacy
    }
}
