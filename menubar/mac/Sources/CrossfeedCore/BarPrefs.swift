// Crossfeed menu bar: what the operator chose to see in the bar itself, saved in app preferences.
// Pure Foundation so the tests can read every rule. The same choices CodexBar offers (which provider,
// used or left, icon or number), cut to the five that matter here.
import Foundation

public struct BarPrefs: Equatable {
    public enum Count: String, CaseIterable { case used, left }
    public enum Icon: String, CaseIterable { case tanks, logo, none }
    /// How much of "Gemini 5h 59%" to print. `.none` leaves the icon alone.
    public enum Text: String, CaseIterable { case full, window, number, none }

    public static let pressed = "pressed"
    public static let refreshChoices = [60, 300, 900]
    /// "tightest" (whichever window of the allowance is closest to its limit) or one window by its short name.
    public static let tightest = "tightest"
    public static let windowChoices = [tightest, "5h", "Week", "Month"]

    /// "pressed" (whatever is closest to its limit) or a provider tab id such as "claude".
    public var show = BarPrefs.pressed
    public var window = BarPrefs.tightest
    public var count = Count.used
    public var icon = Icon.tanks
    public var text = Text.full
    public var refreshSeconds = 60

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

    public func load() -> BarPrefs {
        var prefs = BarPrefs()
        if let show = defaults.string(forKey: Self.prefix + "show"), !show.isEmpty, show.count <= 40 { prefs.show = show }
        if let window = defaults.string(forKey: Self.prefix + "window"), BarPrefs.windowChoices.contains(window) { prefs.window = window }
        prefs.count = defaults.string(forKey: Self.prefix + "count").flatMap(BarPrefs.Count.init) ?? prefs.count
        prefs.icon = defaults.string(forKey: Self.prefix + "icon").flatMap(BarPrefs.Icon.init) ?? prefs.icon
        prefs.text = defaults.string(forKey: Self.prefix + "text").flatMap(BarPrefs.Text.init) ?? prefs.text
        let refresh = defaults.integer(forKey: Self.prefix + "refresh")
        if BarPrefs.refreshChoices.contains(refresh) { prefs.refreshSeconds = refresh }
        return prefs
    }

    public func save(_ prefs: BarPrefs) {
        defaults.set(prefs.show, forKey: Self.prefix + "show")
        defaults.set(prefs.window, forKey: Self.prefix + "window")
        defaults.set(prefs.count.rawValue, forKey: Self.prefix + "count")
        defaults.set(prefs.icon.rawValue, forKey: Self.prefix + "icon")
        defaults.set(prefs.text.rawValue, forKey: Self.prefix + "text")
        defaults.set(prefs.refreshSeconds, forKey: Self.prefix + "refresh")
    }

    public func reset() {
        for key in ["show", "window", "count", "icon", "text", "refresh"] { defaults.removeObject(forKey: Self.prefix + key) }
    }
}
