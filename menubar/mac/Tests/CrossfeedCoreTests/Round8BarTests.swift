import Foundation
import XCTest
@testable import CrossfeedCore

private let now = ISO8601DateFormatter().date(from: "2026-11-26T12:00:00Z")!

private func fixture(_ name: String) throws -> Data {
    try Data(contentsOf: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("Fixtures").appendingPathComponent(name))
}

private func presenter(_ body: String) throws -> Presenter {
    Presenter(glance: try Glance.decode(Data(body.utf8)), loadedAt: now, now: now)
}

/// The operator's case on 26 Nov: Gemini's 5-hour window at 59% was the most pressed allowance and the bar
/// showed "59%" with nothing saying of what.
private let geminiPressed = """
{"schema":1,"title":"Claude 12% · Gemini 59%","pools":[
 {"pool":"claude","name":"Claude","measured":true,"used_percent":12,"limits":[
   {"kind":"window","name":"secondary","title":"Weekly","minutes":10080,"used_percent":12,"reset_at":"2026-12-01T12:00:00Z"}]},
 {"pool":"antigravity-gemini","name":"Gemini","measured":true,"used_percent":59,"limits":[
   {"kind":"window","name":"w","title":"Weekly","minutes":10080,"used_percent":3},
   {"kind":"window","name":"h","title":"5-hour","minutes":300,"used_percent":59,"reset_at":"2026-11-26T14:13:00Z"}]}
]}
"""

final class BarTitleTests: XCTestCase {
    func testTheNumberSaysWhichAllowanceAndWindow() throws {
        let s = try presenter(geminiPressed).barSummary(BarPrefs())
        XCTAssertEqual(s.text, "Gemini 5h 59%")
        XCTAssertEqual(s.name, "Gemini")
        XCTAssertEqual(s.window, "5h")
        XCTAssertEqual(s.logo, .antigravity)
        XCTAssertTrue(s.tooltip.hasPrefix("Gemini 5-hour: 59% used · Resets in 2h 13m"), s.tooltip)
        XCTAssertTrue(s.tooltip.contains("Crossfeed · Claude 12% · Gemini 59%"))
    }

    func testEveryTextChoice() throws {
        let p = try presenter(geminiPressed)
        var prefs = BarPrefs()
        let texts = BarPrefs.Text.allCases.map { text -> String in prefs.text = text; return p.barSummary(prefs).text }
        XCTAssertEqual(texts, ["Gemini 5h 59%", "5h 59%", "59%", ""])
    }

    func testLeftCountsWhatRemainsAndSaysSo() throws {
        let p = try presenter(geminiPressed)
        var prefs = BarPrefs()
        prefs.count = .left
        let s = p.barSummary(prefs)
        XCTAssertEqual(s.text, "Gemini 5h 41% left")
        XCTAssertEqual(s.left, 41)
        XCTAssertEqual(s.right, 88)       // Claude, 12% used
        XCTAssertEqual(s.used, 59)
        prefs.text = .number
        XCTAssertEqual(p.barSummary(prefs).text, "41% left")
    }

    func testAProviderCanBePinnedAndFallsBackWhenUnread() throws {
        let p = try presenter(geminiPressed)
        var prefs = BarPrefs()
        prefs.show = "claude"
        XCTAssertEqual(p.barSummary(prefs).text, "Claude Week 12%")
        XCTAssertEqual(p.barSummary(prefs).right, 59)
        prefs.show = "codex"                // no Codex in this reading: the text names what it shows instead
        XCTAssertEqual(p.barSummary(prefs).text, "Gemini 5h 59%")
    }

    func testResetAndPaceCanFollowTheNumber() throws {
        let p = try presenter(geminiPressed)
        var prefs = BarPrefs()
        prefs.extra = .reset
        XCTAssertEqual(p.barSummary(prefs).text, "Gemini 5h 59% · 2h 13m")
        prefs.extra = .pace                       // Gemini carries no pace: nothing is invented
        XCTAssertEqual(p.barSummary(prefs).text, "Gemini 5h 59%")
        let glance = try Glance.decode(fixture("glance-today.json"))
        let codex = Presenter(glance: glance, loadedAt: now, now: now).barSummary(prefs)
        XCTAssertEqual(codex.text, "Codex Week 88% · 46% over")
        prefs.text = .none
        XCTAssertEqual(p.barSummary(prefs).text, "")   // no text, no suffix
    }

    func testTodaysCostAddsUpAcrossProvidersAndIsNeverInvented() throws {
        var prefs = BarPrefs()
        prefs.extra = .cost
        XCTAssertEqual(try presenter(geminiPressed).barSummary(prefs).text, "Gemini 5h 59%")     // no cost reported
        let priced = try Glance.decode(fixture("glance-round7.json"))
        let s = Presenter(glance: priced, loadedAt: now, now: now).barSummary(prefs)
        XCTAssertEqual(s.text, "Codex Week 88% · US$12.02 today")      // Claude 3.04 + Antigravity 8.22 + Codex 0.76
    }

    func testOnlyProvidersWithAReadingCanBePinned() throws {
        let p = try presenter(geminiPressed)
        XCTAssertEqual(p.barChoices.map(\.id), ["claude", "agy"])
        let none = try presenter(#"{"schema":1,"pools":[{"pool":"openrouter-free","name":"OpenRouter","measured":false}]}"#)
        XCTAssertTrue(none.barChoices.isEmpty)
    }

    func testResetsCanReadAsAClockInTheViewersZone() throws {
        let rome = TimeZone(identifier: "Europe/Rome")!     // UTC+1 in November: 14:13Z is 15:13 local
        let glance = try Glance.decode(Data(geminiPressed.utf8))
        let p = Presenter(glance: glance, loadedAt: now, now: now, resetClock: true, timeZone: rome)
        XCTAssertEqual(p.card(for: "claude")?.sections.first?.rows.first?.resetText, "Resets Tue 13:00")
        XCTAssertEqual(p.card(for: "agy")?.sections.first?.rows.last?.resetText, "Resets today 15:13")
        var prefs = BarPrefs()
        prefs.extra = .reset
        XCTAssertEqual(p.barSummary(prefs).text, "Gemini 5h 59% · 15:13")
        let utc = Presenter(glance: glance, loadedAt: now, now: now, resetClock: true, timeZone: TimeZone(identifier: "UTC")!)
        XCTAssertEqual(utc.barSummary(prefs).text, "Gemini 5h 59% · 14:13")
        // Tomorrow and within the week
        let line = Presenter(glance: glance, loadedAt: now, now: now, resetClock: true, timeZone: rome)
        XCTAssertEqual(line.clockText(now.addingTimeInterval(86400), today: ""), "tomorrow 13:00")
        XCTAssertEqual(line.clockText(now.addingTimeInterval(3 * 86400), today: ""), "Sun 13:00")
        XCTAssertEqual(line.clockText(now.addingTimeInterval(9 * 86400), today: ""), "5 Dec 13:00")
    }

    func testAWindowCanBePinnedAndFallsBackWhenNobodyHasIt() throws {
        let p = try presenter(geminiPressed)
        var prefs = BarPrefs()
        prefs.window = "Week"
        XCTAssertEqual(p.barSummary(prefs).text, "Claude Week 12%")   // Gemini's weekly is 3%
        prefs.show = "agy"
        XCTAssertEqual(p.barSummary(prefs).text, "Gemini Week 3%")
        prefs.window = "Month"                                       // no one has a monthly window
        XCTAssertEqual(p.barSummary(prefs).text, "Gemini 5h 59%")
    }

    func testDeficitLeadsAndKeepsTheOldTanks() throws {
        let glance = try Glance.decode(fixture("glance-today.json"))
        let s = Presenter(glance: glance, loadedAt: now, now: now).barSummary(BarPrefs())
        XCTAssertEqual(s.text, "Codex Week 88%")
        XCTAssertTrue(s.deficit)
        XCTAssertEqual(s.left, 88)
        XCTAssertEqual(s.right, 15)
    }

    func testTheItemIsNeverEmpty() throws {
        var prefs = BarPrefs()
        prefs.icon = .none; prefs.text = .none
        XCTAssertEqual(prefs.effective.icon, .tanks)
        let s = try presenter(geminiPressed).barSummary(prefs)
        XCTAssertEqual(s.icon, .tanks)
        XCTAssertEqual(s.text, "")
        prefs.icon = .none; prefs.text = .number
        XCTAssertEqual(try presenter(geminiPressed).barSummary(prefs).icon, BarPrefs.Icon.none)
    }

    func testLogoFallsBackToTanksWithoutAProviderMark() throws {
        let p = try presenter("""
        {"schema":1,"pools":[{"pool":"some-lab","name":"Some Lab","measured":true,"used_percent":30,"limits":[
          {"kind":"window","title":"Monthly","minutes":43200,"used_percent":30}]}]}
        """)
        var prefs = BarPrefs()
        prefs.icon = .logo
        let s = p.barSummary(prefs)
        XCTAssertEqual(s.icon, .tanks)
        XCTAssertEqual(s.text, "Some Lab Month 30%")
    }

    func testNothingReadSaysNothing() throws {
        let s = try presenter(#"{"schema":1,"pools":[]}"#).barSummary(BarPrefs())
        XCTAssertEqual(s.text, "")
        XCTAssertNil(s.left)
    }

    func testWindowNamesFollowTheirLength() {
        func name(_ minutes: Double?, _ title: String) -> String {
            Presenter.barWindow(Limit(kind: "window", title: title, minutes: minutes)).short
        }
        XCTAssertEqual(name(300, "5-hour"), "5h")
        XCTAssertEqual(name(10080, "Weekly"), "Week")
        XCTAssertEqual(name(43200, "Monthly"), "Month")
        XCTAssertEqual(name(nil, "Monthly"), "Month")
        XCTAssertEqual(name(180, "x"), "3h")
    }
}

final class BarPrefsStoreTests: XCTestCase {
    private func defaults() -> (UserDefaults, String) {
        let suite = "org.crossfeed.tests." + UUID().uuidString
        return (UserDefaults(suiteName: suite)!, suite)
    }

    func testChoicesPersistAcrossAnInstance() {
        let (d, suite) = defaults()
        defer { d.removePersistentDomain(forName: suite) }
        var prefs = BarPrefs()
        prefs.show = "codex"; prefs.window = "Week"; prefs.count = .left; prefs.icon = .logo; prefs.text = .window; prefs.extra = .reset; prefs.reset = .clock; prefs.refreshSeconds = 300
        BarPrefsStore(defaults: d).save(prefs)
        XCTAssertEqual(BarPrefsStore(defaults: UserDefaults(suiteName: suite)!).load(), prefs)
    }

    func testNothingSavedIsTheDefaultAndGarbageIsIgnored() {
        let (d, suite) = defaults()
        defer { d.removePersistentDomain(forName: suite) }
        XCTAssertEqual(BarPrefsStore(defaults: d).load(), BarPrefs())
        d.set("sideways", forKey: "crossfeed.bar.count")
        d.set("hologram", forKey: "crossfeed.bar.icon")
        d.set("emoji", forKey: "crossfeed.bar.extra")
        d.set(7, forKey: "crossfeed.bar.refresh")
        d.set("", forKey: "crossfeed.bar.show")
        XCTAssertEqual(BarPrefsStore(defaults: d).load(), BarPrefs())
    }

    func testResetReturnsToDefault() {
        let (d, suite) = defaults()
        defer { d.removePersistentDomain(forName: suite) }
        var prefs = BarPrefs()
        prefs.text = .number
        let store = BarPrefsStore(defaults: d)
        store.save(prefs)
        store.reset()
        XCTAssertEqual(store.load(), BarPrefs())
    }
}
