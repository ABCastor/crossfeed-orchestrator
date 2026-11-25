import Foundation

/// Only order belongs in preferences; provider membership comes from the latest reading.
public struct TabOrder {
    private static let key = "crossfeed.provider-order"
    private let defaults: UserDefaults

    public init(defaults: UserDefaults) { self.defaults = defaults }

    public func ordered(_ available: [String]) -> [String] {
        let availableIDs = Set(available)
        return Self.merged(saved, available).filter { availableIDs.contains($0) }
    }

    @discardableResult
    public func move(_ source: String, onto target: String, among available: [String]) -> Bool {
        var visible = ordered(available)
        guard source != target, let from = visible.firstIndex(of: source),
              let to = visible.firstIndex(of: target) else { return false }
        visible.remove(at: from)
        visible.insert(source, at: to)
        // Retain the slots of providers that are absent from the current reading.
        var remaining = visible.makeIterator()
        let availableIDs = Set(available)
        let order = Self.merged(saved, available).map { availableIDs.contains($0) ? remaining.next()! : $0 }
        defaults.set(order, forKey: Self.key)
        return true
    }

    private var saved: [String] { defaults.stringArray(forKey: Self.key) ?? [] }

    private static func merged(_ saved: [String], _ available: [String]) -> [String] {
        var seen = Set<String>()
        return (saved + available).filter { seen.insert($0).inserted }
    }
}
