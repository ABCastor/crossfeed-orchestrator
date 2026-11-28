// Crossfeed menu: panel.json, the one preferences file the Mac menu and the web panel share.
//
//   ~/.config/orchestrator/panel.json   (XDG_CONFIG_HOME respected; FLEET_PANEL_PREFS overrides)
//   {"schema":1, "show":"used", "tabs":[...], "hidden_tabs":[...], "tiles":{tab:[ids]}, "hidden_tiles":{tab:[ids]},
//    "appearance":"system|light|dark", "menubar":{"icon":"crossfeed|provider|none"},
//    "refresh":{"on_open":true,"every_minutes":5}, "open_on":"last|<tab>", "last_tab":"<tab>",
//    "budgets":{pool:usd}, "tile_options":{"<tab>/<tile>":{"metric":"cost","days":30}}}
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

/// What a tile's chart plots: the pool's tokens, its cost in USD, or its request count.
public enum ChartMetric: String, CaseIterable {
    case tokens, cost, requests
    public var title: String { self == .tokens ? "Tokens" : (self == .cost ? "Cost" : "Requests") }
}

/// One tile's chart options, as `tile_options` keeps them. A missing field means the tile's own default.
public struct TileOption: Equatable {
    public var metric: ChartMetric?
    public var days: Int?
    public init(metric: ChartMetric? = nil, days: Int? = nil) { self.metric = metric; self.days = days }
}

public struct PanelPrefs: Equatable {
    public enum Show: String, CaseIterable { case used, left }
    /// The look of the menu and Settings: the system's, or fixed light or dark.
    public enum Appearance: String, CaseIterable {
        case system, light, dark
        public var title: String { rawValue.prefix(1).uppercased() + rawValue.dropFirst() }
    }

    public static let schema = 1
    public static let fixedFirst = "overview"
    public static let fixedLast = "other"
    /// The pools billed per token, by prepaid credit or through a relay each have a tab of their own, named by the pool.
    public static let apiTabs = ["openrouter-free", "gemini-metered", "chatgpt-work"]
    public static let defaultTabs = ["overview", "claude", "codex", "copilot", "agy", "go"] + apiTabs + ["other"]
    /// What every provider tab can hold, in the default order. Plugin tiles are added by the operator.
    public static let providerTiles = ["limits", "resets", "tokens", "policy", "credits", "status", "links"]
    /// What an API tab holds: spend against a budget, the balance, the models, the activity chart.
    public static let apiTiles = ["spend", "balance", "models", "activity", "status", "links"]
    /// Overview's status tile is not in the default list: it appears by itself while a provider reports an incident,
    /// and the operator can pin it (then it stays, saying "No incidents").
    public static let overviewTiles = ["alert", "providers", "subscriptions", "tokens", "links"]
    /// Tiles that sit two to a row when next to each other.
    public static let halfTiles: Set<String> = ["tokens", "policy", "credits", "balance", "activity"]
    public static let refreshChoices = [0, 1, 5, 15]
    public static let rangeChoices = [7, 14, 30]
    /// Budget presets the Settings chips offer, in USD a month.
    public static let budgetChoices: [Double] = [10, 20, 50, 100, 200]

    public var show = Show.used
    public var appearance = Appearance.system
    /// The bar item's icon as the file spells it: "crossfeed", "provider" or "none". Nil until the operator chooses.
    public var menubarIcon: String?
    /// Read again when the menu opens, if the last reading is older than a minute.
    public var refreshOnOpen = true
    /// Minutes between background readings; 0 is off.
    public var refreshEveryMinutes = 5
    /// "last" (the tab last used) or a tab id.
    public var openOn = "last"
    public var lastTab: String?
    /// Optional monthly budget per API pool, in USD. The menu only shows it.
    public var budgets: [String: Double] = [:]
    public var tileOptions: [String: TileOption] = [:]
    public var tabs: [String] = PanelPrefs.defaultTabs
    public var hiddenTabs: [String] = []
    public var tiles: [String: [String]] = [:]
    public var hiddenTiles: [String: [String]] = [:]
    /// Keys this version does not know, written back untouched.
    public var extra: [String: JSONValue] = [:]

    public init() {}

    public static func defaultTiles(for tab: String) -> [String] {
        tab == "overview" ? overviewTiles : (tab == "other" ? ["limits"] : (apiTabs.contains(tab) ? apiTiles : providerTiles))
    }

    /// The tile that holds a chart on a tab, if it has one: Activity on an API tab, else Tokens.
    public static func chartTile(on tab: String, available: [String]) -> String? {
        ["activity", "tokens"].first { available.contains($0) }
    }

    public static func optionKey(_ tab: String, _ tile: String) -> String { tab + "/" + tile }

    public func option(_ tab: String, _ tile: String) -> TileOption { tileOptions[Self.optionKey(tab, tile)] ?? TileOption() }

    public mutating func setOption(_ tab: String, _ tile: String, _ change: (inout TileOption) -> Void) {
        var option = self.option(tab, tile)
        change(&option)
        tileOptions[Self.optionKey(tab, tile)] = option == TileOption() ? nil : option
    }

    /// The tab to open on: the saved choice, else the one last used, if it is still shown; else Overview.
    public func startTab(shown: [String]) -> String {
        let wanted = openOn == "last" ? (lastTab ?? PanelPrefs.fixedFirst) : openOn
        return shown.contains(wanted) ? wanted : PanelPrefs.fixedFirst
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
    /// are new to the list. Hidden tiles drop out. A plugin tile shows only where the operator put it. `auto` names tiles
    /// that show by themselves while they have something to say (Overview's status), unless the operator hid them.
    public func tileOrder(_ tab: String, available: [String], auto: [String] = []) -> [String] {
        let present = Set(available), hidden = Set(hiddenTiles[tab] ?? [])
        var seen = Set<String>()
        let defaults = PanelPrefs.defaultTiles(for: tab)
        // An unsaved provider tab shows the built-in tiles it has, in the order they are offered (an API tab's differ from a
        // subscription's); Overview keeps its own order. A built-in tile this version adds sits before Links in a list saved
        // earlier.
        let unsaved = tiles[tab] == nil
        var order = tiles[tab] ?? (tab == PanelPrefs.fixedFirst ? defaults : available.filter { !TileID.isPlugin($0) })
        let known = Set(order)
        let extras = unsaved ? (tab == PanelPrefs.fixedFirst ? available.filter { !TileID.isPlugin($0) } : []) : defaults
        let fresh = (extras + auto).filter { !known.contains($0) }
        if !fresh.isEmpty {
            if let at = order.firstIndex(of: TileID.links) { order.insert(contentsOf: fresh, at: at) } else { order += fresh }
        }
        return order.filter { present.contains($0) && !hidden.contains($0) && seen.insert($0).inserted }
    }

    /// Every tile the settings list shows for a tab: what is on, then what is off.
    public func listed(_ tab: String, builtIns: [String], auto: [String] = []) -> (on: [String], off: [String]) {
        let hidden = Set(hiddenTiles[tab] ?? [])
        var seen = Set<String>()
        var saved = tiles[tab] ?? PanelPrefs.defaultTiles(for: tab)
        let known = Set(saved)
        let fresh = (PanelPrefs.defaultTiles(for: tab) + auto).filter { !known.contains($0) && !TileID.isPlugin($0) }
        if let at = saved.firstIndex(of: TileID.links) { saved.insert(contentsOf: fresh, at: at) } else { saved += fresh }
        let order = saved.filter { id in
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
        appearance = raw["appearance"]?.stringValue.flatMap(Appearance.init) ?? .system
        if let icon = raw["menubar"]?.objectValue?["icon"]?.stringValue, ["crossfeed", "provider", "none"].contains(icon) { menubarIcon = icon }
        if let refresh = raw["refresh"]?.objectValue {
            if case .bool(let on)? = refresh["on_open"] { refreshOnOpen = on }
            if let every = refresh["every_minutes"]?.numberValue, every == every.rounded(), (0...1440).contains(every) { refreshEveryMinutes = Int(every) }
        }
        if let open = raw["open_on"]?.stringValue, open.count <= 80, !open.isEmpty { openOn = open == "last" ? open : PanelIDs.menu(open) }
        if let last = raw["last_tab"]?.stringValue, last.count <= 80, !last.isEmpty { lastTab = PanelIDs.menu(last) }
        for (pool, value) in raw["budgets"]?.objectValue ?? [:] where pool.count <= 80 {
            if let usd = value.numberValue, usd.isFinite, usd > 0, usd <= 1_000_000 { budgets[pool] = usd }
        }
        for (key, value) in raw["tile_options"]?.objectValue ?? [:] where key.count <= 170 {
            guard let object = value.objectValue else { continue }
            let parts = key.split(separator: "/", maxSplits: 1).map(String.init)
            guard parts.count == 2 else { continue }
            var option = TileOption()
            option.metric = object["metric"]?.stringValue.flatMap(ChartMetric.init)
            if let days = object["days"]?.numberValue, PanelPrefs.rangeChoices.contains(Int(days)) { option.days = Int(days) }
            if option != TileOption() { tileOptions[PanelPrefs.optionKey(PanelIDs.menu(parts[0]), parts[1])] = option }
        }
        let saved = strings(raw["tabs"]).map(PanelIDs.menu)
        var seen = Set<String>()
        tabs = (saved + PanelPrefs.defaultTabs).filter { seen.insert($0).inserted }
        hiddenTabs = strings(raw["hidden_tabs"]).map(PanelIDs.menu).filter { $0 != PanelPrefs.fixedFirst }
        tiles = lists(raw["tiles"])
        hiddenTiles = lists(raw["hidden_tiles"]).filter { !$0.value.isEmpty }
        let known: Set<String> = ["schema", "show", "tabs", "hidden_tabs", "tiles", "hidden_tiles", "appearance", "menubar",
                                  "refresh", "open_on", "last_tab", "budgets", "tile_options"]
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
        out["appearance"] = appearance.rawValue
        var bar = (base["menubar"] as? [String: Any]) ?? [:]
        bar["icon"] = menubarIcon
        if bar["icon"] == nil { bar.removeValue(forKey: "icon") }
        if bar.isEmpty { out.removeValue(forKey: "menubar") } else { out["menubar"] = bar }
        var refresh = (base["refresh"] as? [String: Any]) ?? [:]
        refresh["on_open"] = refreshOnOpen
        refresh["every_minutes"] = refreshEveryMinutes
        out["refresh"] = refresh
        out["open_on"] = openOn == "last" ? openOn : PanelIDs.file(openOn)
        if let lastTab { out["last_tab"] = PanelIDs.file(lastTab) }
        out["budgets"] = budgets
        var options: [String: Any] = [:]
        for (key, option) in tileOptions {
            let parts = key.split(separator: "/", maxSplits: 1).map(String.init)
            guard parts.count == 2 else { continue }
            var object: [String: Any] = [:]
            if let metric = option.metric { object["metric"] = metric.rawValue }
            if let days = option.days { object["days"] = days }
            options[PanelIDs.file(parts[0]) + "/" + parts[1]] = object
        }
        out["tile_options"] = options
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

extension PanelMigration {
    static let refreshMarker = "crossfeed.panel.migrated.refresh"

    /// The menu bar page's old 1, 5 or 15 minute choice (app preferences) becomes `refresh.every_minutes`, once, unless the
    /// file already says how often. Returns true when it wrote the file.
    @discardableResult
    public static func moveRefresh(store: PanelPrefsStore, defaults: UserDefaults) -> Bool {
        guard !defaults.bool(forKey: refreshMarker) else { return false }
        let key = "crossfeed.bar.refresh"
        let old = defaults.integer(forKey: key)
        var wrote = false
        if [60, 300, 900].contains(old) {
            var raw = true
            if let data = try? Data(contentsOf: store.url), let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                raw = object["refresh"] == nil
            }
            if raw {
                var prefs = store.load()
                prefs.refreshEveryMinutes = old / 60
                wrote = store.save(prefs)
                guard wrote else { return false }
            }
        }
        defaults.removeObject(forKey: key)
        defaults.set(true, forKey: refreshMarker)
        return wrote
    }
}
