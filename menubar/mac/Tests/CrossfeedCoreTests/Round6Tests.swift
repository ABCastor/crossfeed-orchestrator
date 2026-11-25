import Foundation
import XCTest
@testable import CrossfeedCore

final class Round6Tests: XCTestCase {
    func testProviderDropPersistsAndTemporarilyAbsentProvidersKeepTheirSlot() throws {
        let suite = "org.crossfeed.test." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let original = ["claude", "codex", "agy", "go"]
        let order = TabOrder(defaults: defaults)
        XCTAssertEqual(order.ordered(original), original)
        XCTAssertFalse(order.move("agy", onto: "missing", among: original))
        XCTAssertFalse(order.move("agy", onto: "agy", among: original))
        XCTAssertNil(defaults.persistentDomain(forName: suite))
        XCTAssertTrue(order.move("codex", onto: "claude", among: original))

        let reloadedDefaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        let reloaded = TabOrder(defaults: reloadedDefaults)
        XCTAssertNotNil(reloadedDefaults.persistentDomain(forName: suite))
        XCTAssertEqual(reloaded.ordered(original), ["codex", "claude", "agy", "go"])
        let fewer = ["claude", "agy", "go", "copilot"]
        XCTAssertEqual(reloaded.ordered(fewer), fewer)
        XCTAssertTrue(reloaded.move("claude", onto: "go", among: fewer))
        XCTAssertEqual(order.ordered(original + ["copilot"]), ["codex", "agy", "go", "claude", "copilot"])
    }

    func testAboutUsesTheSignedLoopbackURLAndDoesNotChangeTheLoginQuery() throws {
        let line = "Sign-in link for this launch: http://127.0.0.1:8769/login?key=synthetic-example"
        let login = try XCTUnwrap(ConsolePage.signedURL(from: line))
        let about = try XCTUnwrap(ConsolePage.about.url(from: login))
        XCTAssertEqual(about.host, "127.0.0.1")
        XCTAssertEqual(about.port, 8769)
        XCTAssertEqual(about.path, "/login")
        XCTAssertEqual(about.query, login.query)
        XCTAssertEqual(about.fragment, "about")
        XCTAssertEqual(ConsolePage.providers.url(from: login), login)
        XCTAssertNil(ConsolePage.signedURL(from: line.replacingOccurrences(of: "127.0.0.1", with: "example.invalid")))
        XCTAssertNil(ConsolePage.signedURL(from: line.replacingOccurrences(of: "/login", with: "/")))
        XCTAssertNil(ConsolePage.signedURL(from: line + "&redirect=external"))
        XCTAssertNil(ConsolePage.signedURL(from: "unrelated: " + line))
    }

    func testFiveStopsAndMixedSubscriptionOnlyMovesOnConfirmedWrite() throws {
        XCTAssertEqual(SpendLevel.allCases.map(\.rawValue), ["off", "low", "normal", "high", "forced"])
        XCTAssertEqual(SpendLevel.allCases.map(\.label), ["Off", "Low", "Normal", "High", "Max"])
        let reading = #"{"schema":1,"subscriptions":[{"id":"antigravity","level":"mixed","set_by":"agent","set_at":"2026-11-25T10:55:57Z"}],"pools":[{"pool":"antigravity-gemini","subscription":"antigravity","level":"high","set_by":"agent","set_at":"2026-11-25T10:55:57Z"},{"pool":"antigravity-argon","subscription":"antigravity","level":"normal"},{"pool":"antigravity-3p","subscription":"antigravity","level":"low"},{"pool":"claude","level":"low"}]}"#
        var glance = try Glance.decode(Data(reading.utf8))
        var menu = PolicyEdits()
        let change = PolicyChange(pool: "antigravity", level: .forced)
        XCTAssertEqual(glance.policyLevel(for: change.pool), "mixed")
        XCTAssertFalse(menu.begin(change, current: nil), "a missing policy is not Mixed")
        XCTAssertTrue(menu.begin(change, current: nil, mixed: true))
        XCTAssertEqual(menu.selection(for: change.pool, stored: nil), .forced)
        menu.finish(.failure(.rejected), readingAvailable: true)
        XCTAssertNil(menu.selection(for: change.pool, stored: nil))
        XCTAssertEqual(glance.policyLevel(for: change.pool), "mixed")
        XCTAssertTrue(menu.begin(change, current: nil, mixed: true))
        glance.applyConfirmed(change)
        menu.finish(.success(()), readingAvailable: false)
        XCTAssertEqual(glance.subscriptions.map(\.level), ["forced"])
        XCTAssertEqual(glance.pools.map(\.level), ["forced", "forced", "forced", "low"])
        XCTAssertNil(glance.subscriptions.first?.levelControl)
        XCTAssertNil(glance.pools.first?.levelControl)
        XCTAssertEqual(menu.notices["antigravity"], "Saved · reload for attribution")
    }

    func testProviderExtrasUseOnlyPresentValuesAndKeepActualZero() throws {
        let body = #"{"schema":1,"pools":[{"pool":"codex","credits":{"balance":0,"unit":"credits","expires_at":"2026-11-28T12:00:00Z","renews_at":"2026-11-29T12:00:00Z","source":"fixture"},"status_url":"https://status.openai.com/","usage_cost":{"today_usd":0,"today_tokens":0,"last_30d_tokens":3472}},{"pool":"claude","credits":{"renews_at":"unknown"},"status_url":"javascript:bad"},{"pool":"openrouter-free","archived":[{"model":"retired-id","name":"Retired","archived":true}],"per_model":[{"model":"A","usd_30d":0,"requests":1},{"model":"B","usd_30d":1.25,"requests":4},{"model":"retired-id","usd_30d":100,"requests":9}]}]}"#
        let extras = try Glance.decode(Data(body.utf8)).pools.map(ProviderExtras.init(pool:))
        XCTAssertEqual(extras[0].credits.first, "0 credits")
        XCTAssertTrue(extras[0].credits[1].hasPrefix("Expires on "))
        XCTAssertTrue(extras[0].credits[2].hasPrefix("New credits on "))
        XCTAssertEqual(extras[0].status?.host, "status.openai.com")
        XCTAssertEqual(extras[0].usage.map(\.label), ["Today", "Last 30 days"])
        XCTAssertTrue(extras[0].usage[0].detail.hasSuffix(" · 0 tokens"))
        XCTAssertTrue(extras[0].usage[1].detail.hasSuffix(" tokens"))
        XCTAssertFalse(extras[0].usage[1].detail.contains(" · "))
        XCTAssertTrue(extras[1].credits.isEmpty && extras[1].usage.isEmpty && extras[1].perModel.isEmpty)
        XCTAssertNil(extras[1].status)
        XCTAssertEqual(extras[2].perModel.map(\.label), ["B", "A"])
        XCTAssertTrue(extras[2].perModel[1].detail.hasSuffix(" · 1 request"))
    }
}
