// Crossfeed menu bar: when to tell the operator something. Pure Foundation so the tests can read every rule.
//
// Two optional notifications, both off until the operator turns them on:
//   Running low: once when an allowance window reaches 90% used, and once when it is used up.
//   Reset: once when a window that was over 50% used starts over.
// What was last told is remembered per window and saved, so a relaunch repeats nothing. The first reading of a
// window only sets the baseline: an allowance already at 100% when the app starts is not news.
import Foundation

public struct AlertWindow: Equatable {
    public var key: String        // "claude|secondary"
    public var label: String      // "Claude weekly"
    public var used: Double       // 0...100
    public var resetAt: Date?
    public var resetText: String? // "Resets in 3d 4h", or the clock time when the operator chose clocks
    public var held = 0           // usable limit resets held that clear this window
    public var tab: String?       // the provider's tab, where the reset can be used

    public init(key: String, label: String, used: Double, resetAt: Date?, resetText: String?, held: Int = 0, tab: String? = nil) {
        self.key = key; self.label = label; self.used = used; self.resetAt = resetAt; self.resetText = resetText
        self.held = held; self.tab = tab
    }

    /// "🎟️ 1 held" when a reset is held, else nil.
    public var heldText: String? { held > 0 ? "🎟️ \(held) held" : nil }
}

public struct AlertMemory: Codable, Equatable {
    public var level: Int         // 0 below 90%, 1 from 90%, 2 used up
    public var used: Double
    public var peak: Double       // the most used since this window last started over
    public var resetAt: Date?
}

public struct AlertState: Codable, Equatable {
    public var windows: [String: AlertMemory] = [:]
    public init() {}
}

public struct AlertEvent: Equatable {
    public enum Kind: String { case low, out, reset }
    public var kind: Kind
    public var id: String
    public var title: String
    public var body: String
    /// A reset is held for this window: the notice offers Use, which opens the provider's tab on the reset.
    public var offersUse = false
    public var tab: String?
}

public enum AlertEngine {
    public static let lowAt = 90.0
    public static let usedUpAt = Presenter.usedUp
    public static let resetAfter = 50.0

    /// Every window with a reading, on pools that are still in use. A window that is not running has none.
    public static func windows(of presenter: Presenter) -> [AlertWindow] {
        var found: [AlertWindow] = []
        for pool in presenter.glance.pools where pool.measured && !pool.isRetired {
            let name = Presenter.barName(pool)
            var limits = pool.limits.filter { $0.kind == "window" && $0.isActive && $0.usedPercent != nil }
            if limits.isEmpty, pool.limits.isEmpty, let used = pool.usedPercent {
                limits = [Limit(kind: "window", title: "Usage", usedPercent: used, resets: pool.resets, resetAt: pool.resetAt)]
            }
            for (index, limit) in limits.enumerated() {
                guard let used = limit.usedPercent else { continue }
                var label = "\(name) \(Presenter.barWindow(limit).long)"
                if let scope = limit.scope, !scope.isEmpty { label += " · " + scope }
                found.append(AlertWindow(
                    key: "\(pool.pool)|\(limit.name ?? limit.title ?? String(index))|\(limit.scope ?? "")", label: label,
                    used: min(100, max(0, used)), resetAt: limit.resetAt.flatMap { Presenter.iso.date(from: $0) },
                    resetText: presenter.resetText(limit), held: limit.held, tab: Presenter.groupID(pool)))
            }
        }
        return found
    }

    /// One reading in, the new memory and what to say out. Memory always moves on, whether or not the
    /// operator wants the notification, so turning one on later does not release a burst of old news.
    public static func step(_ state: AlertState, _ windows: [AlertWindow], now: Date, low: Bool, reset: Bool)
        -> (state: AlertState, events: [AlertEvent]) {
        var next = state
        var events: [AlertEvent] = []
        for window in windows {
            let level = window.used >= usedUpAt ? 2 : (window.used >= lowAt ? 1 : 0)
            guard let before = state.windows[window.key] else {
                next.windows[window.key] = AlertMemory(level: level, used: window.used, peak: window.used, resetAt: window.resetAt)
                continue
            }
            let passed = before.resetAt.map { $0 <= now } ?? false
            let startedOver = before.peak > resetAfter && window.used < before.used && (passed || window.used <= 5)
            var memory = AlertMemory(level: level, used: window.used, peak: max(before.peak, window.used),
                                     resetAt: window.resetAt ?? before.resetAt)
            let stamp = Int((window.resetAt ?? before.resetAt ?? now).timeIntervalSince1970)
            if startedOver {
                memory.peak = window.used
                memory.resetAt = window.resetAt
                if reset {
                    events.append(AlertEvent(kind: .reset, id: "\(window.key)|reset|\(stamp)", title: "\(window.label) reset",
                                             body: "Ready to use again."))
                }
            } else if level > before.level, low {
                // A held reset changes the news: the window is not about to run out, there is a way on.
                let when = window.resetText ?? ""
                let held = window.heldText.map { " · " + $0 }
                let use = window.held > 0
                if level == 2 {
                    events.append(AlertEvent(kind: .out, id: "\(window.key)|out|\(stamp)", title: "\(window.label) used up" + (held ?? ""),
                                             body: use ? "Use a reset to carry on. " + when : when, offersUse: use, tab: window.tab))
                } else {
                    events.append(AlertEvent(kind: .low, id: "\(window.key)|low|\(stamp)",
                                             title: "\(window.label) at \(Int(window.used.rounded()))%" + (held ?? ""),
                                             body: use ? "A reset is held. " + when : when, offersUse: use, tab: window.tab))
                }
            }
            next.windows[window.key] = memory
        }
        return (next, events)
    }
}

/// The last thing told, per window, kept in app preferences.
public struct AlertStore {
    static let key = "crossfeed.alerts.state"
    private let defaults: UserDefaults

    public init(defaults: UserDefaults) { self.defaults = defaults }

    public func load() -> AlertState {
        defaults.data(forKey: Self.key).flatMap { try? JSONDecoder().decode(AlertState.self, from: $0) } ?? AlertState()
    }

    public func save(_ state: AlertState) {
        if let data = try? JSONEncoder().encode(state) { defaults.set(data, forKey: Self.key) }
    }
}
