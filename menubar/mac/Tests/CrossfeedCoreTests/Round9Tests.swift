import Foundation
import XCTest
@testable import CrossfeedCore

private let now = ISO8601DateFormatter().date(from: "2026-11-26T16:20:00Z")!
private let rome = TimeZone(identifier: "Europe/Rome")!

private func fixture() throws -> Glance {
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("Fixtures/glance-round9.json")
    return try Glance.decode(Data(contentsOf: url))
}

private func presenter(_ body: String, at date: Date = now) throws -> Presenter {
    Presenter(glance: try Glance.decode(Data(body.utf8)), loadedAt: date, now: date, timeZone: rome)
}

final class Round9PrefsTests: XCTestCase {
    private func defaults() -> (UserDefaults, String) {
        let suite = "org.crossfeed.tests." + UUID().uuidString
        return (UserDefaults(suiteName: suite)!, suite)
    }

    func testTheBarDefaultsToTheLogoWithTheWindow() {
        let prefs = BarPrefs()
        XCTAssertEqual(prefs.icon, .logo)
        XCTAssertEqual(prefs.text, .window)
        XCTAssertEqual(prefs.refreshSeconds, 300)
        XCTAssertFalse(prefs.notifyLow)
        XCTAssertFalse(prefs.notifyReset)
    }

    func testTheOldTanksWithWindowPickBecomesTheLogoOnceAndAMinuteBecomesFive() {
        let (d, suite) = defaults()
        defer { d.removePersistentDomain(forName: suite) }
        d.set("tanks", forKey: "crossfeed.bar.icon")
        d.set("window", forKey: "crossfeed.bar.text")
        d.set(60, forKey: "crossfeed.bar.refresh")
        let store = BarPrefsStore(defaults: d)
        var prefs = store.load()
        XCTAssertEqual(prefs.icon, .logo)
        XCTAssertEqual(prefs.refreshSeconds, 300)
        // Migrated for good: the choice is saved, and a later pick of tanks is respected.
        XCTAssertEqual(d.string(forKey: "crossfeed.bar.icon"), "logo")
        prefs.icon = .tanks
        store.save(prefs)
        XCTAssertEqual(store.load().icon, .tanks)
    }

    func testOtherSavedChoicesAreLeftAlone() {
        let (d, suite) = defaults()
        defer { d.removePersistentDomain(forName: suite) }
        d.set("tanks", forKey: "crossfeed.bar.icon")
        d.set("full", forKey: "crossfeed.bar.text")
        d.set(900, forKey: "crossfeed.bar.refresh")
        let prefs = BarPrefsStore(defaults: d).load()
        XCTAssertEqual(prefs.icon, .tanks)
        XCTAssertEqual(prefs.text, .full)
        XCTAssertEqual(prefs.refreshSeconds, 900)
    }

    func testNotificationChoicesPersist() {
        let (d, suite) = defaults()
        defer { d.removePersistentDomain(forName: suite) }
        var prefs = BarPrefs()
        prefs.notifyLow = true
        BarPrefsStore(defaults: d).save(prefs)
        let again = BarPrefsStore(defaults: UserDefaults(suiteName: suite)!).load()
        XCTAssertTrue(again.notifyLow)
        XCTAssertFalse(again.notifyReset)
    }
}

final class Round9BarTests: XCTestCase {
    func testTheTanksAreTheShownProvidersShortAndWeeklyWindows() throws {
        let p = Presenter(glance: try fixture(), loadedAt: now, now: now, timeZone: rome)
        var prefs = BarPrefs()
        prefs.show = "agy"
        let gemini = p.barSummary(prefs)
        XCTAssertEqual(gemini.name, "Gemini")
        XCTAssertEqual(gemini.left, 59)        // 5-hour
        XCTAssertEqual(gemini.right, 1)        // weekly
        prefs.show = "go"
        let go = p.barSummary(prefs)
        XCTAssertEqual(go.left, 4)
        XCTAssertEqual(go.right, 6)
    }

    func testAWindowThatIsNotRunningIsAnEmptyOutline() throws {
        let p = Presenter(glance: try fixture(), loadedAt: now, now: now, timeZone: rome)
        var prefs = BarPrefs()
        prefs.show = "claude"
        let s = p.barSummary(prefs)
        XCTAssertNil(s.left)                   // Claude's 5-hour has not started
        XCTAssertEqual(s.right, 36)
        XCTAssertEqual(s.text, "Week 36%")
        XCTAssertEqual(s.logo, .claude)
    }

    func testLeftCountsTheTanksAsWhatRemainsButKeepsOutlinesEmpty() throws {
        let p = Presenter(glance: try fixture(), loadedAt: now, now: now, timeZone: rome)
        var prefs = BarPrefs()
        prefs.show = "claude"; prefs.count = .left
        let s = p.barSummary(prefs)
        XCTAssertNil(s.left)
        XCTAssertEqual(s.right, 64)
    }

    func testAPoolWhoseModelsAreWithdrawnNeverLeadsTheBar() throws {
        // Argon is at 63% and in deficit, the most pressed; its model carries retired_on, so it is skipped.
        let glance = try fixture()
        let argon = glance.pools.first { $0.pool == "antigravity-argon" }!
        XCTAssertTrue(argon.isRetired)
        let p = Presenter(glance: glance, loadedAt: now, now: now, timeZone: rome)
        XCTAssertNotEqual(p.barSummary(BarPrefs()).name, "Argon")
        XCTAssertFalse(p.pressedPools().contains { $0.pool.pool == "antigravity-argon" })
    }

    func testArchivedModelsAloneRetireAPoolButOneLiveModelKeepsIt() throws {
        let archived = try presenter("""
        {"schema":1,"pools":[{"pool":"old","name":"Old","measured":true,"used_percent":90,"models":[{"name":"A","archived":true}],
          "limits":[{"kind":"window","title":"Weekly","minutes":10080,"used_percent":90}]},
          {"pool":"codex","name":"Codex","measured":true,"used_percent":10,"models":[{"name":"B"}],
          "limits":[{"kind":"window","title":"Weekly","minutes":10080,"used_percent":10}]}]}
        """)
        XCTAssertEqual(archived.barSummary(BarPrefs()).name, "Codex")
        let mixed = try presenter("""
        {"schema":1,"pools":[{"pool":"codex","name":"Codex","measured":true,"used_percent":90,"models":[{"name":"A","archived":true},{"name":"B"}],
          "limits":[{"kind":"window","title":"Weekly","minutes":10080,"used_percent":90}]}]}
        """)
        XCTAssertEqual(mixed.barSummary(BarPrefs()).name, "Codex")
    }

    func testUsedUpSaysSoWithTheResetTime() throws {
        let p = Presenter(glance: try fixture(), loadedAt: now, now: now, timeZone: rome)
        var prefs = BarPrefs()
        prefs.show = "codex"
        XCTAssertEqual(p.barSummary(prefs).tooltip, "Codex used up · resets Sun 29 Nov 12:04")
        XCTAssertEqual(p.card(for: "codex")?.sections.first?.rows.first?.usedText, "Used up")
    }

    func testTheBarSentenceDoesNotRepeatTheReadersTitle() throws {
        let p = Presenter(glance: try fixture(), loadedAt: now, now: now, timeZone: rome)
        XCTAssertFalse(p.barSummary(BarPrefs()).tooltip.contains("full:"))
    }
}

final class Round9ClaudeCardTests: XCTestCase {
    func testEveryResetTheGlanceHasIsOnTheCard() throws {
        let p = Presenter(glance: try fixture(), loadedAt: now, now: now, timeZone: rome)
        let section = try XCTUnwrap(p.card(for: "claude")?.sections.first)
        XCTAssertEqual(section.rows.map(\.title), ["5-hour", "Weekly", "Weekly · Fable only"])
        let five = section.rows[0]
        XCTAssertTrue(five.inactive)
        XCTAssertNil(five.bar)
        XCTAssertEqual(five.resetText, "Starts with your next message")
        XCTAssertEqual(section.rows[1].resetText, "Resets in 4d 20h")
        XCTAssertNil(section.note, "an idle window is not a missing reading")
    }

    func testExtraUsageIsOneLineWithItsDate() throws {
        let p = Presenter(glance: try fixture(), loadedAt: now, now: now, timeZone: rome)
        let rows = try XCTUnwrap(p.card(for: "claude")?.sections.first?.extras.creditRows)
        XCTAssertEqual(rows.count, 2)
        XCTAssertTrue(rows[0].label.hasPrefix("Extra usage left · "), rows[0].label)
        XCTAssertTrue(rows[0].detail.hasPrefix("Renews"), rows[0].detail)
        XCTAssertTrue(rows[1].detail.hasPrefix("Expires"), rows[1].detail)
    }

    func testAnIdleWindowIsNotCountedAsPressure() throws {
        let p = try presenter("""
        {"schema":1,"pools":[{"pool":"claude","name":"Claude","measured":true,"used_percent":0,"limits":[
          {"kind":"window","title":"5-hour","minutes":300,"active":false,"used_percent":0,"note":"Starts with your next message"}]}]}
        """)
        XCTAssertEqual(p.tabs.first?.used, nil)
        XCTAssertNil(p.barSummary(BarPrefs()).left)
    }
}

final class Round9AlertTests: XCTestCase {
    private func window(_ used: Double, reset: Date? = now.addingTimeInterval(3 * 86400)) -> AlertWindow {
        AlertWindow(key: "codex|weekly|", label: "Codex weekly", used: used, resetAt: reset, resetText: "Resets in 3d 0h")
    }

    private func run(_ steps: [(Double, TimeInterval)], low: Bool = true, reset: Bool = true,
                     state: AlertState = AlertState(), resetsAt: Date? = now.addingTimeInterval(3 * 86400)) -> (AlertState, [[AlertEvent.Kind]]) {
        var state = state
        var told: [[AlertEvent.Kind]] = []
        for (used, offset) in steps {
            let result = AlertEngine.step(state, [window(used, reset: resetsAt)], now: now.addingTimeInterval(offset), low: low, reset: reset)
            state = result.state
            told.append(result.events.map(\.kind))
        }
        return (state, told)
    }

    func testTheFirstReadingIsOnlyABaseline() {
        XCTAssertEqual(run([(100, 0)]).1, [[]])
        XCTAssertEqual(run([(100, 0), (100, 60), (100, 120)]).1, [[], [], []])
    }

    func testRunningLowFiresOnceAt90AndOnceWhenUsedUp() {
        let told = run([(40, 0), (85, 60), (91, 120), (93, 180), (100, 240), (100, 300)]).1
        XCTAssertEqual(told, [[], [], [.low], [], [.out], []])
    }

    func testJumpingStraightToUsedUpSaysOnlyThat() {
        XCTAssertEqual(run([(40, 0), (100, 60)]).1, [[], [.out]])
    }

    func testNothingRepeatsAcrossARelaunch() {
        let first = run([(40, 0), (95, 60)])
        XCTAssertEqual(first.1.last, [.low])
        // The app restarts: memory comes back from preferences, and the next readings say nothing new.
        let suite = "org.crossfeed.tests." + UUID().uuidString
        let d = UserDefaults(suiteName: suite)!
        defer { d.removePersistentDomain(forName: suite) }
        AlertStore(defaults: d).save(first.0)
        let restored = AlertStore(defaults: UserDefaults(suiteName: suite)!).load()
        XCTAssertEqual(restored, first.0)
        XCTAssertEqual(run([(95, 120), (96, 180)], state: restored).1, [[], []])
        XCTAssertEqual(run([(100, 120)], state: restored).1, [[.out]])
    }

    func testResetFiresOnceWhenAnAllowanceOverHalfStartsOver() {
        // Reset time passes between the second and third readings.
        let reset = now.addingTimeInterval(100)
        let told = run([(80, 0), (82, 50), (3, 200), (4, 260)], resetsAt: reset).1
        XCTAssertEqual(told, [[], [], [.reset], []])
    }

    func testAnAllowanceUnderHalfDoesNotAnnounceItsReset() {
        let reset = now.addingTimeInterval(100)
        XCTAssertEqual(run([(40, 0), (45, 50), (2, 200)], resetsAt: reset).1, [[], [], []])
    }

    func testAUsedUpAllowanceThatResetsNotifiesAndCanBeToldAgainNextCycle() {
        let reset = now.addingTimeInterval(100)
        let first = run([(95, 0), (100, 50), (0, 200)], resetsAt: reset)
        XCTAssertEqual(first.1, [[], [.out], [.reset]])
        let next = now.addingTimeInterval(9 * 86400)
        XCTAssertEqual(run([(92, 300)], state: first.0, resetsAt: next).1, [[.low]])
    }

    func testTogglesGateTheNotificationNotTheMemory() {
        // Both off: nothing is said, but the memory moves on, so turning them on later releases no old news.
        let off = run([(40, 0), (95, 60), (100, 120)], low: false, reset: false)
        XCTAssertEqual(off.1, [[], [], []])
        XCTAssertEqual(off.0.windows["codex|weekly|"]?.level, 2)
        XCTAssertEqual(run([(100, 180)], state: off.0).1, [[]])
    }

    func testWindowsComeFromLivePoolsAndRunningWindowsOnly() throws {
        let p = Presenter(glance: try fixture(), loadedAt: now, now: now, timeZone: rome)
        let labels = AlertEngine.windows(of: p).map(\.label)
        XCTAssertTrue(labels.contains("Claude weekly"))
        XCTAssertTrue(labels.contains("Claude weekly · Fable only"))
        XCTAssertFalse(labels.contains("Claude 5-hour"), "a window that is not running has no reading")
        XCTAssertFalse(labels.contains { $0.hasPrefix("Argon") }, "a withdrawn pool is not announced")
    }
}

final class Round9PaintTests: XCTestCase {
    private func summary(_ text: String) -> BarSummary {
        BarSummary(text: text, left: nil, right: 10, deficit: false, tooltip: text)
    }

    func testAnUnchangedItemIsNeverRepaintedAndABusyOneWaits() {
        var gate = PaintGate()
        XCTAssertTrue(gate.shouldPaint(summary("Week 36%"), busy: false))
        XCTAssertFalse(gate.shouldPaint(summary("Week 36%"), busy: false), "same summary: no repaint, so no tooltip or click disturbance")
        XCTAssertFalse(gate.shouldPaint(summary("Week 37%"), busy: true), "a click or an open menu is in progress")
        XCTAssertTrue(gate.shouldPaint(summary("Week 37%"), busy: false), "asked again afterwards")
        gate.forget()
        XCTAssertTrue(gate.shouldPaint(summary("Week 37%"), busy: false))
    }
}
