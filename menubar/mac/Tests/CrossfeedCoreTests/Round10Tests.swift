import Foundation
import XCTest
@testable import CrossfeedCore

private func fixture(_ name: String) throws -> Glance {
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("Fixtures").appendingPathComponent(name)
    return try Glance.decode(Data(contentsOf: url))
}

private let now = ISO8601DateFormatter().date(from: "2026-11-26T16:20:00Z")!

private func suite() -> UserDefaults {
    let name = "org.crossfeed.test.r10." + UUID().uuidString
    let defaults = UserDefaults(suiteName: name)!
    defaults.removePersistentDomain(forName: name)
    return defaults
}

final class TabLayoutTests: XCTestCase {
    func testNothingSavedShowsEveryTabAndKeepsTheOrder() {
        let defaults = suite()
        let layout = TabLayoutStore(defaults: defaults).load()
        XCTAssertEqual(layout, TabLayout())
        XCTAssertTrue(layout.isVisible("claude"))
        defaults.set(["codex", "claude"], forKey: "crossfeed.provider-order")
        XCTAssertEqual(TabOrder(defaults: defaults).ordered(["claude", "codex", "agy"]), ["codex", "claude", "agy"])
    }

    func testHiddenAndTheOverviewChoicePersist() {
        let defaults = suite()
        var layout = TabLayout()
        layout.toggle("copilot")
        layout.hiddenInOverview = true
        TabLayoutStore(defaults: defaults).save(layout)
        let back = TabLayoutStore(defaults: defaults).load()
        XCTAssertEqual(back.hidden, ["copilot"])
        XCTAssertEqual(back.overviewIDs(["claude", "copilot"]), ["claude", "copilot"])
        var off = back
        off.hiddenInOverview = false
        XCTAssertEqual(off.overviewIDs(["claude", "copilot"]), ["claude"])
        off.toggle("copilot")
        XCTAssertTrue(off.isVisible("copilot"))
        TabLayoutStore(defaults: defaults).reset()
        XCTAssertEqual(TabLayoutStore(defaults: defaults).load(), TabLayout())
    }

    func testOtherStaysLastAndCannotBeMoved() {
        let defaults = suite()
        defaults.set(["other", "claude", "codex"], forKey: "crossfeed.provider-order")
        let order = TabOrder(defaults: defaults)
        let all = ["claude", "codex", "other"]
        XCTAssertEqual(order.ordered(all), ["claude", "codex", "other"])
        XCTAssertFalse(order.move("other", onto: "claude", among: all))
        XCTAssertFalse(order.move("claude", onto: "other", among: all))
        XCTAssertTrue(order.move("codex", onto: "claude", among: all))
        XCTAssertEqual(order.ordered(all), ["codex", "claude", "other"])
    }
}

final class OverviewTests: XCTestCase {
    func testOneRowPerProviderWithItsTightestWindow() throws {
        let presenter = Presenter(glance: try fixture("glance-round9.json"), loadedAt: now, now: now)
        let ids = presenter.tabs.map(\.id)
        let rows = presenter.overviewRows(order: ids)
        XCTAssertEqual(rows.map(\.id), ids.filter { id in rows.contains { $0.id == id } })
        let claude = try XCTUnwrap(rows.first { $0.id == "claude" })
        XCTAssertNotNil(claude.used)
        XCTAssertTrue(claude.usedText.hasSuffix("%") || claude.usedText == "Used up")
        XCTAssertNotNil(claude.reset)
        XCTAssertEqual(presenter.overviewRows(order: ["codex", "claude"]).map(\.id), ["codex", "claude"])
        XCTAssertEqual(presenter.overviewRows(order: ["nope"]), [])
    }

    func testAnAllowanceUsedUpIsFlaggedNotPercent() throws {
        let presenter = Presenter(glance: try fixture("glance-today.json"), loadedAt: now, now: now)
        let rows = presenter.overviewRows(order: presenter.tabs.map(\.id))
        for row in rows where (row.used ?? 0) >= Presenter.usedUp {
            XCTAssertEqual(row.usedText, "Used up")
            XCTAssertEqual(row.flag, .usedUp)
        }
    }
}

final class PlanPriceTests: XCTestCase {
    private func glance(_ spend: String) throws -> Glance {
        let body = #"{"schema":1,"title":"t","pools":[],"subscriptions":[],"subscription_spend":\#(spend)}"#
        return try Glance.decode(Data(body.utf8))
    }

    func testAPlanShowsItsPriceFromTheDefaults() throws {
        let g = try glance(#"{"plans":[{"id":"claude","name":"Claude Max 20x","amount":"183.00","currency":"GBP","period":"month","source":"default"},{"id":"x","name":"Free thing"}],"monthly":[{"currency":"GBP","total":"183.00"}],"unpriced":1,"unsplit":1,"unavailable":false}"#)
        let spend = Presenter(glance: g, loadedAt: now, now: now).spend
        XCTAssertEqual(spend.plans, [PlanLine(name: "Claude Max 20x", price: "£183/mo", priced: true),
                                     PlanLine(name: "Free thing", price: "No price", priced: false)])
    }

    func testPeriodsCurrenciesAndNumbers() throws {
        func price(_ amount: String, _ currency: String?, _ period: String?) -> String? {
            Presenter.planPrice(PlanPrice(id: "a", amount: amount, currency: currency, period: period))
        }
        XCTAssertEqual(price("20", "EUR", "month"), "€20/mo")
        XCTAssertEqual(price("12.5", "USD", "year"), "US$12.50/yr")
        XCTAssertEqual(price("9", nil, nil), "9/mo")
        XCTAssertNil(price("abc", "GBP", "month"))
    }

    func testNoPricesYetOnlyWhenNothingIsPriced() throws {
        let none = try glance(#"{"monthly":[],"unpriced":4,"unsplit":0,"unavailable":false}"#)
        XCTAssertEqual(Presenter(glance: none, loadedAt: now, now: now).spend.lines, ["No prices yet"])
        let priced = try glance(#"{"plans":[{"id":"c","name":"Claude","amount":"100","currency":"GBP","period":"month"}],"monthly":[{"currency":"GBP","total":"100.00"}],"unpriced":0,"unsplit":1,"unavailable":false}"#)
        let spend = Presenter(glance: priced, loadedAt: now, now: now).spend
        XCTAssertFalse(spend.lines.contains("No prices yet")); XCTAssertEqual(spend.lines.first, "Per month: GBP 100.00")
        XCTAssertEqual(spend.plans.first?.price, "£100/mo")
    }
}

final class ConsoleUsageLinkTests: XCTestCase {
    func testUsageOpensTheUsagePageAtThePoolWithoutAKeyOfItsOwn() throws {
        let login = try XCTUnwrap(ConsolePage.signedURL(from: "Sign-in link for this launch: http://127.0.0.1:8123/login?key=abc"))
        let url = try XCTUnwrap(ConsolePage.usage(pool: "claude").url(from: login))
        XCTAssertEqual(url.host, "127.0.0.1")
        XCTAssertEqual(url.path, "/login")
        XCTAssertEqual(url.fragment, "claude")
        XCTAssertEqual(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.map(\.name), ["key", "next"])
        XCTAssertNil(ConsolePage.usage(pool: nil).url(from: login)?.fragment)
        XCTAssertNil(ConsolePage.usage(pool: "../x y").url(from: login)?.fragment)
    }
}
