import Foundation
import XCTest
@testable import CrossfeedCore

private let now = ISO8601DateFormatter().date(from: "2026-11-26T09:30:00Z")!

private func presenter(_ pools: String, subscriptions: String = "[]") throws -> Presenter {
    let body = #"{"schema":1,"title":"","subscriptions":\#(subscriptions),"pools":\#(pools)}"#
    return Presenter(glance: try Glance.decode(Data(body.utf8)), loadedAt: now, now: now)
}

private func extras(_ pool: String) throws -> ProviderExtras {
    let glance = try Glance.decode(Data(#"{"schema":1,"pools":[\#(pool)]}"#.utf8))
    return ProviderExtras(pool: glance.pools[0])
}

final class Round7GapsTests: XCTestCase {
    func testAbsentFieldsShowNothing() throws {
        let plain = try extras(#"{"pool":"claude","name":"Claude"}"#)
        XCTAssertNil(plain.incident)
        XCTAssertTrue(plain.billing.isEmpty)
        XCTAssertTrue(plain.modelQuota.isEmpty)
        XCTAssertTrue(plain.costByModel.isEmpty)
        // An all-clear level is not a badge.
        XCTAssertNil(try extras(#"{"pool":"claude","name":"Claude","incident":{"level":"none"}}"#).incident)
    }

    func testIncidentNamesTheLevelAndItsCause() throws {
        let e = try extras(#"{"pool":"codex","name":"Codex","incident":{"level":"minor","text":"Elevated errors","affected":["Codex"],"since":"2026-11-25T17:27:00Z"}}"#)
        let incident = try XCTUnwrap(e.incident)
        XCTAssertEqual(incident.title, "Degraded")
        XCTAssertFalse(incident.isSevere)
        XCTAssertTrue(incident.detail.hasPrefix("Elevated errors · Codex · since "))
        let major = try extras(#"{"pool":"claude","name":"Claude","incident":{"level":"major"}}"#).incident
        XCTAssertEqual(major?.title, "Major incident")
        XCTAssertEqual(major?.isSevere, true)
    }

    func testBillingShowsPeriodAndGrantButLeavesCreditUseToTheConsole() throws {
        let e = try extras(#"{"pool":"codex","name":"Codex","billing":{"plan":"pro","period":"monthly","renews_at":"2026-12-04T10:58:38Z","grants":[{"credits":12048,"granted":62500,"expires_at":"2027-01-01T00:00:00Z"}],"credits_today":1098,"credits_7d":2816,"credits_30d":42051}}"#)
        XCTAssertEqual(e.billing.count, 2)
        XCTAssertTrue(e.billing[0].hasPrefix("Pro · Monthly renews "))
        XCTAssertTrue(e.billing[1].hasPrefix("Funded credits 12,048 of 62,500 left, expire "))
        XCTAssertFalse(e.billing.contains { $0.hasPrefix("Credits used") }, "usage reports are on the console's /usage page")
    }

    func testModelQuotaAndAntigravityCostByModel() throws {
        let e = try extras(#"{"pool":"antigravity-argon","name":"Argon","model_quota":[{"label":"Gemini 4 Argon","used_percent":27.6,"reset_at":"2026-11-26T14:13:49Z","shared_with":"Gemini 5-hour"},{"label":"No reading"}],"usage_cost":{"today_usd":8.2,"last_30d_usd":8.47,"by_model":[{"model":"Gemini 4 Argon","tokens":14000000,"usd":8.4},{"model":"Unpriced","tokens":5}]}}"#)
        XCTAssertEqual(e.modelQuota.count, 1, "a quota with no percent is hidden")
        XCTAssertTrue(e.modelQuota[0].detail.hasPrefix("28% used · resets "))
        XCTAssertTrue(e.modelQuota[0].detail.hasSuffix("shares Gemini 5-hour"))
        XCTAssertEqual(e.costByModel.map(\.label), ["Gemini 4 Argon", "Unpriced"])
        XCTAssertEqual(e.costByModel[1].detail, "5 tokens")
    }

    func testMalformedNewFieldsAreIgnoredNotFatal() throws {
        let e = try extras(#"{"pool":"codex","name":"Codex","incident":"bad","billing":[1],"model_quota":{"x":1},"usage_cost":{"by_model":"bad","today_usd":1}}"#)
        XCTAssertNil(e.incident)
        XCTAssertTrue(e.billing.isEmpty)
        XCTAssertTrue(e.modelQuota.isEmpty)
        XCTAssertTrue(e.costByModel.isEmpty)
        XCTAssertEqual(e.usage.count, 1)
    }

    func testAntigravityStatesAnIncidentOnceOnTheGroupCard() throws {
        let incident = #""incident":{"level":"minor","text":"Gemini degraded"}"#
        let p = try presenter(#"[{"pool":"antigravity-gemini","name":"Gemini","subscription":"antigravity",\#(incident)},{"pool":"antigravity-argon","name":"Argon","subscription":"antigravity",\#(incident)},{"pool":"antigravity-3p","name":"3P","subscription":"antigravity",\#(incident)}]"#)
        let card = try XCTUnwrap(p.card(for: "agy"))
        XCTAssertEqual(card.sections.compactMap(\.extras.incident).count, 1)
    }
}
