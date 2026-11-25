import Foundation
import XCTest
@testable import CrossfeedCore

private func fixtureURL(_ name: String) -> URL {
    URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("Fixtures").appendingPathComponent(name)
}

private func fixture(_ name: String = "glance-today.json") throws -> Data {
    try Data(contentsOf: fixtureURL(name))
}

private let fixtureNow = ISO8601DateFormatter().date(from: "2026-11-25T09:53:57Z")!

private func json(_ body: String) -> Data { Data(body.utf8) }

final class GlanceDecodingTests: XCTestCase {
    func testDecodesTodaysRealShape() throws {
        let glance = try Glance.decode(fixture())
        XCTAssertEqual(glance.schema, 1)
        XCTAssertEqual(glance.pools.count, 10)
        let codex = try XCTUnwrap(glance.pools.first { $0.pool == "codex" })
        XCTAssertEqual(codex.usedPercent, 88)
        XCTAssertEqual(codex.pace?.status, "deficit")
        XCTAssertEqual(codex.pace?.points, 46)
        XCTAssertEqual(codex.pace?.words, "46% in deficit, runs out in 9h 39m")
        XCTAssertEqual(codex.limits.first?.minutes, 10080)
        XCTAssertEqual(glance.spend?.unpriced, 4)
        XCTAssertEqual(glance.spend?.monthly, [])
    }

    func testUnreadPoolsKeepTheirReason() throws {
        let glance = try Glance.decode(fixture())
        let argon = try XCTUnwrap(glance.pools.first { $0.pool == "antigravity-argon" })
        XCTAssertNil(argon.usedPercent)
        XCTAssertTrue(argon.measured)
        XCTAssertEqual(argon.gaugeError, "no matching rate windows")
        XCTAssertTrue(argon.limits.allSatisfy { $0.usedPercent == nil } || argon.limits.isEmpty)
        let gemini = try XCTUnwrap(glance.pools.first { $0.pool == "antigravity-gemini" })
        XCTAssertEqual(gemini.usedPercent, 1)
        XCTAssertEqual(gemini.limits.first?.resetUnknown, true)
        XCTAssertNotNil(gemini.gaugeError, "a pool with a reading and a failed refresh keeps both")
    }

    func testMissingSpendDecodesAsNilNotAFailure() throws {
        let glance = try Glance.decode(json(#"{"schema":1,"title":"","pools":[]}"#))
        XCTAssertNil(glance.spend)
        let presenter = Presenter(glance: glance, loadedAt: fixtureNow, now: fixtureNow)
        XCTAssertEqual(presenter.spend.lines, ["Spend unavailable"])
        XCTAssertTrue(presenter.tabs.isEmpty)
    }

    func testUnavailableSpendIsSaidNotHidden() throws {
        let glance = try Glance.decode(json(#"{"schema":1,"pools":[],"subscription_spend":{"monthly":[],"unpriced":0,"unsplit":0,"unavailable":true}}"#))
        XCTAssertEqual(Presenter(glance: glance, loadedAt: fixtureNow, now: fixtureNow).spend.lines, ["Spend unavailable"])
    }

    func testPricedSpendShowsTheOperatorsShare() throws {
        let body = #"{"schema":1,"pools":[],"subscription_spend":{"monthly":[{"currency":"EUR","total":"20.00","operator":"10.00"}],"unpriced":2,"unsplit":0,"unavailable":false}}"#
        let spend = Presenter(glance: try Glance.decode(json(body)), loadedAt: fixtureNow, now: fixtureNow).spend
        XCTAssertEqual(spend.lines, ["This month: EUR 20.00", "Your share: EUR 10.00"])
        XCTAssertEqual(spend.share, 0.5)
        XCTAssertEqual(spend.note, "2 plans have no price yet")
    }

    func testUnsetShareIsNotZero() throws {
        let body = #"{"schema":1,"pools":[],"subscription_spend":{"monthly":[{"currency":"EUR","total":"20.00","operator":null}],"unpriced":0,"unsplit":1,"unavailable":false}}"#
        let spend = Presenter(glance: try Glance.decode(json(body)), loadedAt: fixtureNow, now: fixtureNow).spend
        XCTAssertEqual(spend.lines, ["This month: EUR 20.00", "Your share: not set"])
        XCTAssertNil(spend.share)
    }

    func testRefusesOtherSchemas() {
        XCTAssertThrowsError(try Glance.decode(json(#"{"schema":2,"pools":[]}"#))) {
            XCTAssertEqual($0 as? GlanceError, .unsupportedSchema(2))
        }
        XCTAssertThrowsError(try Glance.decode(json("not json"))) { XCTAssertEqual($0 as? GlanceError, .malformed) }
        XCTAssertThrowsError(try Glance.decode(json(#"{"pools":[]}"#)))
    }

    func testGaugeErrorAsObjectOrBoolean() throws {
        let body = #"{"schema":1,"pools":[{"pool":"a","name":"A","measured":true,"gauge_error":{"reason":"signed out"},"limits":[]},{"pool":"b","name":"B","gauge_error":true,"limits":[]},{"pool":"c","name":"C","gauge_error":null,"limits":[]}]}"#
        let pools = try Glance.decode(json(body)).pools
        XCTAssertEqual(pools.map(\.gaugeError), ["signed out", "no reading", nil])
    }

    func testToleratesUnknownKeysAndMissingOptionalFields() throws {
        let body = #"{"schema":1,"future":{"x":1},"pools":[{"pool":"p","name":"P","limits":[{"kind":"window","used_percent":3,"new_field":true}]}]}"#
        let glance = try Glance.decode(json(body))
        XCTAssertEqual(glance.pools[0].limits[0].usedPercent, 3)
        XCTAssertFalse(glance.pools[0].measured)
    }
}

final class PresentationTests: XCTestCase {
    private func presenter(_ name: String = "glance-today.json") throws -> Presenter {
        Presenter(glance: try Glance.decode(fixture(name)), loadedAt: fixtureNow, now: fixtureNow)
    }

    func testTabsFollowCodexBarOrderAndGroupAGY() throws {
        let tabs = try presenter().tabs
        XCTAssertEqual(tabs.map(\.id), ["claude", "codex", "copilot", "agy", "go", "other"])
        XCTAssertEqual(tabs.map(\.title), ["Claude", "Codex", "Copilot", "AGY", "OpenCode", "Other"])
    }

    func testOpensOnTheDeficit() throws {
        XCTAssertEqual(try presenter().defaultTab, "codex")
    }

    func testCodexDeficitIsVisibleInTheBarModel() throws {
        let card = try XCTUnwrap(presenter().card(for: "codex"))
        let row = try XCTUnwrap(card.sections.first?.rows.first)
        XCTAssertEqual(row.title, "Weekly")
        XCTAssertEqual(row.usedText, "88% used")
        let bar = try XCTUnwrap(row.bar)
        XCTAssertEqual(bar.pace, .deficit)
        XCTAssertTrue(bar.hot)
        XCTAssertEqual(try XCTUnwrap(bar.expected), 42.2, accuracy: 0.001)
        XCTAssertEqual(row.paceText, "Pace: 46% in deficit · runs out in 9h 39m")
        XCTAssertEqual(row.resetText, "Resets in 4d 1h")   // 349,818 s after the fixture's clock
        XCTAssertEqual(card.badge, "Conserve")
    }

    func testSurplusIsNotHot() throws {
        let row = try XCTUnwrap(presenter().card(for: "copilot")?.sections.first?.rows.first)
        XCTAssertEqual(row.bar?.pace, .surplus)
        XCTAssertEqual(row.bar?.hot, false)
        XCTAssertEqual(row.paceText, "Pace: 81% surplus")
        XCTAssertEqual(row.title, "Monthly · Premium")
    }

    func testScopedWindowIsLabelled() throws {
        let rows = try XCTUnwrap(presenter().card(for: "claude")?.sections.first?.rows)
        XCTAssertEqual(rows.map(\.title), ["Weekly", "Weekly · Fable only"])
        XCTAssertEqual(rows[1].paceText, "Pace: on pace")
    }

    func testAGYShowsUnreadPoolsWithTheirReasonAndTheReadOne() throws {
        let card = try XCTUnwrap(presenter().card(for: "agy"))
        XCTAssertEqual(card.title, "Antigravity")
        XCTAssertEqual(card.sections.map(\.title), ["Argon", "AGY Claude/GPT", "Gemini"])
        let argon = card.sections[0]
        XCTAssertTrue(argon.rows.isEmpty, "an unread pool draws no bar")
        XCTAssertEqual(argon.badge, "Not read")
        XCTAssertEqual(argon.note, "No matching rate windows")
        let gemini = card.sections[2]
        XCTAssertEqual(gemini.rows.map(\.title), ["5-hour", "Weekly"])
        XCTAssertEqual(gemini.rows[0].resetText, "Reset unknown")
        XCTAssertEqual(gemini.rows[1].usedText, "1% used")
        XCTAssertTrue(gemini.note?.hasPrefix("Last reading kept") == true)
    }

    func testPoolsWithoutQuotaWindowsLandInOther() throws {
        let card = try XCTUnwrap(presenter().card(for: "other"))
        XCTAssertEqual(card.sections.count, 3)
        XCTAssertEqual(card.sections.filter { $0.rows.isEmpty }.map(\.badge), ["No quota window", "No quota window"])
        XCTAssertEqual(card.sections[1].rows.first?.usedText, "0% used", "a capped pool with a reading keeps its bar")
        XCTAssertNil(card.sections[1].badge)
    }

    func testCountdownWording() {
        let base = fixtureNow
        XCTAssertEqual(Presenter.countdown(to: base.addingTimeInterval(3 * 86400 + 20 * 3600 + 59), now: base), "Resets in 3d 20h")
        XCTAssertEqual(Presenter.countdown(to: base.addingTimeInterval(3 * 3600 + 53 * 60), now: base), "Resets in 3h 53m")
        XCTAssertEqual(Presenter.countdown(to: base.addingTimeInterval(20), now: base), "Resets in 1m")
        XCTAssertEqual(Presenter.countdown(to: base.addingTimeInterval(-1), now: base), "Resetting")
    }

    func testCountdownMovesWithTheClockNotTheFixture() throws {
        let glance = try Glance.decode(fixture())
        let later = Presenter(glance: glance, loadedAt: fixtureNow, now: fixtureNow.addingTimeInterval(3600))
        XCTAssertEqual(later.card(for: "codex")?.sections.first?.rows.first?.resetText, "Resets in 4d 0h")
        XCTAssertEqual(later.card(for: "codex")?.subtitle, "Updated 62 min ago")
    }

    func testMenuBarSummaryLeadsWithTheDeficit() throws {
        let s = try presenter().summary
        XCTAssertEqual(s.text, "88%")
        XCTAssertTrue(s.deficit)
        XCTAssertEqual(s.left, 88)
        XCTAssertEqual(s.right, 15)
        XCTAssertTrue(s.tooltip.contains("Codex 88%"))
    }

    func testEmptyGlanceSaysNothingIsRead() throws {
        let p = Presenter(glance: try Glance.decode(json(#"{"schema":1,"pools":[]}"#)), loadedAt: fixtureNow, now: fixtureNow)
        XCTAssertEqual(p.summary.text, "")
        XCTAssertNil(p.defaultTab)
    }
}
