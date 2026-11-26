// Crossfeed menu bar: what the operator chose to see in the bar and how resets read, saved in app preferences.
// Pure Foundation so the tests can read every rule. The same choices CodexBar offers (which provider,
// used or left, icon or number), cut to the five that matter here.
import Foundation

public struct BarPrefs: Equatable {
    public enum Count: String, CaseIterable { case used, left }
    public enum Icon: String, CaseIterable { case tanks, logo, none }
    /// How much of "Gemini 5h 59%" to print. `.none` leaves the icon alone.
    public enum Text: String, CaseIterable { case full, window, number, none }

    /// Something short after the number: when the window resets, how far ahead of an even burn it is, or today's estimated cost.
    public enum Extra: String, CaseIterable { case none, reset, pace, cost }

    /// How a reset is written, in the card and after the bar number: "in 4d 8h" or the clock time it happens.
    public enum Reset: String, CaseIterable { case countdown, clock }

    public static let pressed = "pressed"
    public static let refreshChoices = [60, 300, 900]
    /// "tightest" (whichever window of the allowance is closest to its limit) or one window by its short name.
    public static let tightest = "tightest"
    public static let windowChoices = [tightest, "5h", "Week", "Month"]

    /// "pressed" (whatever is closest to its limit) or a provider tab id such as "claude".
    public var show = BarPrefs.pressed
    public var window = BarPrefs.tightest
    public var count = Count.used
    public var icon = Icon.logo
    public var text = Text.window
    public var extra = Extra.none
    public var reset = Reset.countdown
    public var refreshSeconds = 300
    /// Notifications, off until the operator turns one on: an allowance nearly or fully used, and one that resets.
    public var notifyLow = false
    public var notifyReset = false

    public init() {}

    /// An item with no icon and no text would be invisible; the tanks stay.
    public var effective: BarPrefs {
        var copy = self
        if copy.icon == .none && copy.text == .none { copy.icon = .tanks }
        return copy
    }

    /// "Gemini 5h 59%", "5h 59%", "59%" or "41% left". Pure so the settings pane can print each choice.
    public static func format(name: String, window: String, used: Double, count: Count, text: Text) -> String {
        let value = Int((count == .used ? used : 100 - used).rounded())
        let amount = "\(max(0, min(100, value)))%" + (count == .left ? " left" : "")
        switch text {
        case .full: return "\(name) \(window) \(amount)"
        case .window: return "\(window) \(amount)"
        case .number: return amount
        case .none: return ""
        }
    }
}

/// UserDefaults-backed, tolerant of anything another version or a hand edit left there.
public struct BarPrefsStore {
    static let prefix = "crossfeed.bar."
    private let defaults: UserDefaults

    public init(defaults: UserDefaults) { self.defaults = defaults }

    /// Version of the saved choices. 2: the logo with the window is the default, and a reading every 5 minutes.
    static let version = 2

    public func load() -> BarPrefs {
        var prefs = BarPrefs()
        if let show = defaults.string(forKey: Self.prefix + "show"), !show.isEmpty, show.count <= 40 { prefs.show = show }
        if let window = defaults.string(forKey: Self.prefix + "window"), BarPrefs.windowChoices.contains(window) { prefs.window = window }
        prefs.count = defaults.string(forKey: Self.prefix + "count").flatMap(BarPrefs.Count.init) ?? prefs.count
        prefs.icon = defaults.string(forKey: Self.prefix + "icon").flatMap(BarPrefs.Icon.init) ?? prefs.icon
        prefs.text = defaults.string(forKey: Self.prefix + "text").flatMap(BarPrefs.Text.init) ?? prefs.text
        prefs.extra = defaults.string(forKey: Self.prefix + "extra").flatMap(BarPrefs.Extra.init) ?? prefs.extra
        prefs.reset = defaults.string(forKey: Self.prefix + "reset").flatMap(BarPrefs.Reset.init) ?? prefs.reset
        let refresh = defaults.integer(forKey: Self.prefix + "refresh")
        if BarPrefs.refreshChoices.contains(refresh) { prefs.refreshSeconds = refresh }
        prefs.notifyLow = defaults.bool(forKey: Self.prefix + "notifyLow")
        prefs.notifyReset = defaults.bool(forKey: Self.prefix + "notifyReset")
        if defaults.integer(forKey: Self.prefix + "version") < Self.version {
            // Once: the tanks with the window text (the old pick) become the logo, and a 1-minute reading becomes 5.
            if prefs.icon == .tanks && prefs.text == .window { prefs.icon = .logo; defaults.set(prefs.icon.rawValue, forKey: Self.prefix + "icon") }
            if prefs.refreshSeconds == 60 { prefs.refreshSeconds = 300; defaults.set(300, forKey: Self.prefix + "refresh") }
            migrate()
        }
        return prefs
    }

    private func migrate() { defaults.set(Self.version, forKey: Self.prefix + "version") }

    public func save(_ prefs: BarPrefs) {
        defaults.set(prefs.show, forKey: Self.prefix + "show")
        defaults.set(prefs.window, forKey: Self.prefix + "window")
        defaults.set(prefs.count.rawValue, forKey: Self.prefix + "count")
        defaults.set(prefs.icon.rawValue, forKey: Self.prefix + "icon")
        defaults.set(prefs.text.rawValue, forKey: Self.prefix + "text")
        defaults.set(prefs.extra.rawValue, forKey: Self.prefix + "extra")
        defaults.set(prefs.reset.rawValue, forKey: Self.prefix + "reset")
        defaults.set(prefs.refreshSeconds, forKey: Self.prefix + "refresh")
        defaults.set(prefs.notifyLow, forKey: Self.prefix + "notifyLow")
        defaults.set(prefs.notifyReset, forKey: Self.prefix + "notifyReset")
        migrate()
    }

    public func reset() {
        for key in ["show", "window", "count", "icon", "text", "extra", "reset", "refresh", "notifyLow", "notifyReset"] { defaults.removeObject(forKey: Self.prefix + key) }
    }
}

/// When the status item may be repainted. Setting its image or title while a click is landing, or while its menu
/// is open, can swallow the click or leave the item half drawn; an unchanged summary is never painted at all.
public struct PaintGate {
    public private(set) var painted: BarSummary?

    public init() {}

    /// True when `summary` should be applied now. A refused paint is simply asked again on the next tick.
    public mutating func shouldPaint(_ summary: BarSummary, busy: Bool) -> Bool {
        guard summary != painted, !busy else { return false }
        painted = summary
        return true
    }

    /// Forget what is on screen, so the next paint always happens (after a display change, a Space switch, wake).
    public mutating func forget() { painted = nil }
}
