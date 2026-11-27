// Crossfeed menu: which provider tabs are shown, and whether hidden ones still count in Overview.
// Order lives in TabOrder; this holds only visibility. Nothing saved means every tab is shown, which
// is how every existing install migrates: all visible, in the order it already has.
import Foundation

public struct TabLayout: Equatable {
    public var hidden: Set<String> = []
    /// Hidden providers still get a row in Overview when this is on. Off by default: hidden means hidden.
    public var hiddenInOverview = false

    public init(hidden: Set<String> = [], hiddenInOverview: Bool = false) {
        self.hidden = hidden; self.hiddenInOverview = hiddenInOverview
    }

    public func isVisible(_ id: String) -> Bool { !hidden.contains(id) }

    /// The providers Overview lists, in the given order.
    public func overviewIDs(_ order: [String]) -> [String] {
        hiddenInOverview ? order : order.filter(isVisible)
    }

    public mutating func toggle(_ id: String) {
        if hidden.contains(id) { hidden.remove(id) } else { hidden.insert(id) }
    }
}

public struct TabLayoutStore {
    static let hiddenKey = "crossfeed.tabs.hidden"
    static let overviewKey = "crossfeed.tabs.hiddenInOverview"
    private let defaults: UserDefaults

    public init(defaults: UserDefaults) { self.defaults = defaults }

    public func load() -> TabLayout {
        let ids = (defaults.stringArray(forKey: Self.hiddenKey) ?? []).filter { !$0.isEmpty && $0.count <= 40 }
        return TabLayout(hidden: Set(ids.prefix(32)), hiddenInOverview: defaults.bool(forKey: Self.overviewKey))
    }

    public func save(_ layout: TabLayout) {
        defaults.set(layout.hidden.sorted(), forKey: Self.hiddenKey)
        defaults.set(layout.hiddenInOverview, forKey: Self.overviewKey)
    }

    public func reset() {
        defaults.removeObject(forKey: Self.hiddenKey)
        defaults.removeObject(forKey: Self.overviewKey)
    }
}
