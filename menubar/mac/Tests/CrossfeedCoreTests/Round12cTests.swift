import Foundation
import XCTest
@testable import CrossfeedCore

private func fixtureData(_ name: String) throws -> Data {
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("Fixtures").appendingPathComponent(name)
    return try Data(contentsOf: url)
}

private let now = ISO8601DateFormatter().date(from: "2026-11-27T10:02:00Z")!
private let rome = TimeZone(identifier: "Europe/Rome")!

/// The round 12c fixture: Claude weekly at 96% in deficit with one held reset (and the Fable window with its own).
private func presenter(edit: ((inout [String: Any]) -> Void)? = nil) throws -> Presenter {
    var data = try fixtureData("glance-round12c.json")
    if let edit {
        var object = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        edit(&object)
        data = try JSONSerialization.data(withJSONObject: object)
    }
    return Presenter(glance: try Glance.decode(data), loadedAt: now, now: now, timeZone: rome)
}

/// Edits every limit of one pool.
private func editLimits(_ pool: String, _ change: @escaping (inout [String: Any]) -> Void) -> (inout [String: Any]) -> Void {
    { object in
        var pools = object["pools"] as! [[String: Any]]
        for i in pools.indices where pools[i]["pool"] as? String == pool {
            var limits = pools[i]["limits"] as! [[String: Any]]
            for j in limits.indices { change(&limits[j]) }
            pools[i]["limits"] = limits
        }
        object["pools"] = pools
    }
}

/// Nothing else runs out, so the Overview line is about Claude alone.
private func quiet(_ object: inout [String: Any]) {
    var pools = object["pools"] as! [[String: Any]]
    for i in pools.indices where pools[i]["pool"] as? String != "claude" {
        var limits = (pools[i]["limits"] as? [[String: Any]]) ?? []
        for j in limits.indices { limits[j]["pace"] = NSNull() }
        pools[i]["limits"] = limits
        pools[i]["pace"] = NSNull()
        // Codex is used up with resets held; the test of the held line is about Claude's weekly.
        if pools[i]["pool"] as? String == "codex" { pools[i]["limits"] = [] ; pools[i]["used_percent"] = 10 }
    }
    object["pools"] = pools
}

final class Round12cGlanceTests: XCTestCase {
    func testResetsHeldAndCapacityDecodeAndAreOptional() throws {
        let p = try presenter()
        let claude = p.glance.pools.first { $0.pool == "claude" }!
        XCTAssertEqual(claude.limits.first { $0.name == "weekly" }?.held, 1)
        XCTAssertEqual(claude.limits.first { $0.name == "session" }?.held, 0)
        XCTAssertEqual(claude.resetCapacityPercent, 100)
        let old = try Glance.decode(fixtureData("glance-round12.json"))
        XCTAssertTrue(old.pools.flatMap(\.limits).allSatisfy { $0.held == 0 })
        XCTAssertNil(old.pools.first { $0.pool == "claude" }?.resetCapacityPercent)
    }

    func testAWindowWithAHeldResetDoesNotRunOutAndSaysTheTicketInstead() throws {
        let p = try presenter()
        let claude = p.glance.pools.first { $0.pool == "claude" }!
        let weekly = p.limitLines(claude, show: .used).first { $0.id.hasSuffix(":weekly") }!
        XCTAssertNil(weekly.runsOut)
        XCTAssertEqual(weekly.heldText, "🎟️ 1 held")
        XCTAssertFalse(weekly.hot, "a held reset is the way on: the window is not alarming")
        XCTAssertTrue(weekly.accessibility.contains("1 limit reset held"))
        // The same window without the held reset runs out.
        let bare = try presenter(edit: editLimits("claude") { $0.removeValue(forKey: "resets_held") })
        let line = bare.limitLines(bare.glance.pools.first { $0.pool == "claude" }!, show: .used).first { $0.id.hasSuffix(":weekly") }!
        XCTAssertEqual(line.runsOut, "Runs out in 2h 30m")
        XCTAssertTrue(line.hot)
        XCTAssertNil(line.heldText)
    }

    func testUsedUpWithAHeldResetStaysUsedUpAndNamesTheTicket() throws {
        let p = try presenter(edit: editLimits("claude") { if $0["name"] as? String == "weekly" { $0["used_percent"] = 100 } })
        let line = p.limitLines(p.glance.pools.first { $0.pool == "claude" }!, show: .used).first { $0.id.hasSuffix(":weekly") }!
        XCTAssertEqual(line.figure, "Used up")
        XCTAssertEqual(line.heldText, "🎟️ 1 held")
        XCTAssertTrue(line.hot)
    }

    func testTheOverviewLineSaysHeldNotRunsOut() throws {
        let p = try presenter(edit: quiet)
        let alert = try XCTUnwrap(p.overviewPage(order: p.tabs.map(\.id), show: .used).alert)
        XCTAssertEqual(alert.text, "Claude 96% · 🎟️ 1 held")
        XCTAssertFalse(alert.text.lowercased().contains("runs out"))
        XCTAssertEqual(alert.tab, "claude")
        XCTAssertFalse(alert.hot)
        // Used up: still held, and now it is a hot line.
        let out = try presenter(edit: { object in
            quiet(&object)
            editLimits("claude") { if $0["name"] as? String == "weekly" { $0["used_percent"] = 100 } }(&object)
        })
        let used = try XCTUnwrap(out.overviewPage(order: out.tabs.map(\.id), show: .used).alert)
        XCTAssertEqual(used.text, "Claude used up · 🎟️ 1 held")
        XCTAssertTrue(used.hot)
        // Without the held reset the old words come back.
        let bare = try presenter(edit: { object in
            quiet(&object)
            editLimits("claude") { $0.removeValue(forKey: "resets_held") }(&object)
        })
        XCTAssertEqual(bare.overviewPage(order: bare.tabs.map(\.id), show: .used).alert?.text, "Claude runs out in 2h 30m")
    }

    func testARealRunOutElsewhereStillLeadsTheOverviewLine() throws {
        let p = try presenter()
        let alert = try XCTUnwrap(p.overviewPage(order: p.tabs.map(\.id), show: .used).alert)
        XCTAssertFalse(alert.text.hasPrefix("Claude"), "Claude's held window is not the one that runs out: \(alert.text)")
    }

    func testTheProviderRowAndTheBarTooltipNameTheTicket() throws {
        let p = try presenter()
        let claude = p.glance.pools.first { $0.pool == "claude" }!
        let row = p.row(claude.limits.first { $0.name == "weekly" }!, poolID: "claude", index: 1)
        XCTAssertEqual(row.paceText?.contains("🎟️ 1 held"), true)
        XCTAssertEqual(row.paceText?.lowercased().contains("runs out"), false)
    }
}

final class Round12cAlertTests: XCTestCase {
    func testRunningLowWithAHeldResetOffersUseAndNeverSaysRunsOut() {
        let before = AlertWindow(key: "claude|weekly|", label: "Claude weekly", used: 70, resetAt: nil, resetText: "Resets in 3d 4h", held: 1, tab: "claude")
        var after = before; after.used = 96
        let first = AlertEngine.step(AlertState(), [before], now: now, low: true, reset: false)
        let result = AlertEngine.step(first.state, [after], now: now, low: true, reset: false)
        let event = try! XCTUnwrap(result.events.first)
        XCTAssertEqual(event.title, "Claude weekly at 96% · 🎟️ 1 held")
        XCTAssertTrue(event.offersUse)
        XCTAssertEqual(event.tab, "claude")
        XCTAssertFalse((event.title + event.body).lowercased().contains("runs out"))
    }

    func testUsedUpWithAHeldResetSaysSoAndWithoutOneNothingChanges() {
        var window = AlertWindow(key: "claude|weekly|", label: "Claude weekly", used: 96, resetAt: nil, resetText: "Resets in 3d 4h", held: 1, tab: "claude")
        let first = AlertEngine.step(AlertState(), [window], now: now, low: true, reset: false)
        window.used = 100
        let held = AlertEngine.step(first.state, [window], now: now, low: true, reset: false).events.first!
        XCTAssertEqual(held.title, "Claude weekly used up · 🎟️ 1 held")
        XCTAssertTrue(held.offersUse)
        window.held = 0
        let plain = AlertEngine.step(first.state, [window], now: now, low: true, reset: false).events.first!
        XCTAssertEqual(plain.title, "Claude weekly used up")
        XCTAssertFalse(plain.offersUse)
    }

    func testTheWindowsReadFromAGlanceCarryTheHeldCount() throws {
        let p = try presenter()
        let weekly = AlertEngine.windows(of: p).first { $0.key.hasPrefix("claude|weekly") }
        XCTAssertEqual(weekly?.held, 1)
        XCTAssertEqual(weekly?.tab, "claude")
    }
}

final class Round12cPanelTests: XCTestCase {
    private func tempFile() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("crossfeed-r12c-\(UUID().uuidString)").appendingPathComponent("panel.json")
    }

    func testAutoUseResetsDefaultsToOffAndSurvivesARestart() throws {
        XCTAssertEqual(PanelPrefs().autoUseResets, .off)
        let store = PanelPrefsStore(url: tempFile())
        var panel = store.load()
        panel.autoUseResets = .whenUsedUp
        store.save(panel)
        XCTAssertEqual(PanelPrefsStore(url: store.url).load().autoUseResets, .whenUsedUp)
        let json = try JSONSerialization.jsonObject(with: Data(contentsOf: store.url)) as! [String: Any]
        XCTAssertEqual(json["auto_use_resets"] as? String, "when_used_up")
    }

    func testABadAutoUseValueIsOffAndTheWordsReadAsChips() throws {
        let url = tempFile()
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(#"{"schema":1,"auto_use_resets":"always"}"#.utf8).write(to: url)
        XCTAssertEqual(PanelPrefsStore(url: url).load().autoUseResets, .off)
        XCTAssertEqual(PanelPrefs.AutoUseResets.off.title, "Ask first")
        XCTAssertEqual(PanelPrefs.AutoUseResets.whenUsedUp.title, "Use when used up")
    }

    func testTheThemeSwitchCyclesSystemLightDarkAndWritesAppearance() throws {
        XCTAssertEqual(PanelPrefs.Appearance.system.next, .light)
        XCTAssertEqual(PanelPrefs.Appearance.light.next, .dark)
        XCTAssertEqual(PanelPrefs.Appearance.dark.next, .system)
        let store = PanelPrefsStore(url: tempFile())
        var panel = store.load()
        for expected in ["light", "dark", "system"] {
            panel.appearance = panel.appearance.next
            store.save(panel)
            let json = try JSONSerialization.jsonObject(with: Data(contentsOf: store.url)) as! [String: Any]
            XCTAssertEqual(json["appearance"] as? String, expected)
        }
    }
}
