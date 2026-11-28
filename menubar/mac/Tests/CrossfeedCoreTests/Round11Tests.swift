import CoreGraphics
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

private func presenter(_ name: String = "glance-round11.json", edit: ((inout [String: Any]) -> Void)? = nil) throws -> Presenter {
    var data = try fixtureData(name)
    if let edit {
        var object = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        edit(&object)
        data = try JSONSerialization.data(withJSONObject: object)
    }
    return Presenter(glance: try Glance.decode(data), loadedAt: now, now: now, timeZone: rome)
}

private func tempFile(_ name: String = "panel.json") -> URL {
    FileManager.default.temporaryDirectory.appendingPathComponent("crossfeed-test-\(UUID().uuidString)").appendingPathComponent(name)
}

// MARK: BlockMotion (the 1-D rule the spike proved) and TileMotion (the same rule in two dimensions)

final class BlockMotionTests: XCTestCase {
    func testTheMotionNumbersAreTheSpikes() {
        XCTAssertEqual(BlockMotion.settle, 0.2)
        XCTAssertEqual(BlockMotion.settleReduced, 0.15)
        XCTAssertEqual(BlockMotion.slop, 7)
        XCTAssertEqual(TileMotion.gap, 6)
    }

    func testTopsStackInOrderWithTheGap() {
        XCTAssertEqual(BlockMotion.tops(order: [0, 1, 2], heights: [10, 20, 30], gap: 4), [0, 14, 38])
        XCTAssertEqual(BlockMotion.tops(order: [2, 0, 1], heights: [10, 20, 30], gap: 4), [34, 48, 0])
    }

    func testOrderMovesOneBlockIntoASlot() {
        XCTAssertEqual(BlockMotion.order([0, 1, 2, 3], moving: 3, to: 0), [3, 0, 1, 2])
        XCTAssertEqual(BlockMotion.order([0, 1, 2, 3], moving: 0, to: 9), [1, 2, 3, 0], "past the end clamps")
    }

    func testTheCarriedBlockFollowsThePointerClampedToTheFirstAndLastSlot() {
        let heights: [CGFloat] = [10, 20, 30]
        let first = BlockMotion.frame(order: [0, 1, 2], dragged: 1, top: -50, heights: heights, gap: 4)
        XCTAssertEqual(first.tops[1], 0)
        XCTAssertEqual(first.nearest, 0)
        let last = BlockMotion.frame(order: [0, 1, 2], dragged: 1, top: 500, heights: heights, gap: 4)
        XCTAssertEqual(last.nearest, 2)
        XCTAssertEqual(last.tops[1], BlockMotion.stop(order: [0, 1, 2], dragged: 1, slot: 2, heights: heights, gap: 4))
    }

    func testNeighboursBlendBetweenTheTwoStatesEitherSideOfThePointer() {
        let heights: [CGFloat] = [10, 20, 30]
        let rest = BlockMotion.frame(order: [0, 1, 2], dragged: 1, top: 14, heights: heights, gap: 4)
        XCTAssertEqual(rest.tops, [0, 14, 38])
        let half = BlockMotion.frame(order: [0, 1, 2], dragged: 1, top: 7, heights: heights, gap: 4)
        XCTAssertEqual(half.tops[1], 7)
        XCTAssertEqual(half.tops[0], 12, accuracy: 0.001, "block 0 is halfway between its two places (24 and 0)")
        XCTAssertEqual(half.nearest, 1, "at the midpoint the later state wins")
    }
}

final class TileMotionTests: XCTestCase {
    private let m = TileMetrics(half: [false, true, true, false], full: [40, 30, 30, 20], halfHeight: [40, 50, 60, 20], width: 300, gap: 6)

    func testTwoHalvesShareARowAndTheRowTakesTheTallerHeight() {
        let boxes = TileMotion.view(order: [0, 1, 2, 3], metrics: m)
        XCTAssertEqual(boxes[0], TileBox(x: 0, y: 0, w: 300, h: 40))
        XCTAssertEqual(boxes[1], TileBox(x: 0, y: 46, w: 147, h: 60))
        XCTAssertEqual(boxes[2], TileBox(x: 153, y: 46, w: 147, h: 60))
        XCTAssertEqual(boxes[3], TileBox(x: 0, y: 112, w: 300, h: 20))
    }

    func testAHalfTileAloneSpansTheRow() {
        let boxes = TileMotion.view(order: [1, 0, 2, 3], metrics: m)
        XCTAssertEqual(boxes[1].w, 300)
        XCTAssertEqual(boxes[1].h, 30, "a half tile alone takes its full-width height")
        XCTAssertEqual(boxes[2].w, 300)
    }

    func testTheHeightIsWhereTheLastRowEnds() {
        XCTAssertEqual(TileMotion.height(order: [0, 1, 2, 3], metrics: m), 132)
    }

    func testAGroupStaysTogether() {
        // Tiles 1 and 2 are the group (Overview's providers): a member moves inside it, anything else goes around it.
        let group: [Int?] = [nil, 0, 0, 0, nil]
        XCTAssertEqual(TileMotion.legalSlots(order: [0, 1, 2, 3, 4], dragged: 2, group: group), [1, 2, 3])
        XCTAssertEqual(TileMotion.legalSlots(order: [0, 1, 2, 3, 4], dragged: 0, group: group), [0, 3, 4], "before or after the group, never inside it")
        XCTAssertEqual(TileMotion.legalSlots(order: [0, 1, 2, 3, 4], dragged: 4, group: group), [0, 1, 4])
        XCTAssertEqual(TileMotion.legalSlots(order: [0, 1, 2], dragged: 1, group: nil), [0, 1, 2])
    }

    func testAtAStateTheLayoutIsExactlyThatState() {
        let order = [0, 1, 2, 3]
        let stop = TileMotion.stop(order: order, dragged: 3, slot: 0, metrics: m)
        XCTAssertEqual(stop, CGPoint(x: 0, y: 0))
        let frame = TileMotion.frame(order: order, dragged: 3, anchor: stop, metrics: m)
        XCTAssertEqual(frame.nearest, 0)
        XCTAssertEqual(frame.nearestAnchor, stop)
        XCTAssertEqual(frame.boxes, TileMotion.view(order: TileMotion.order(order, moving: 3, to: 0), metrics: m).enumerated().map { $0.offset == 3 ? TileBox(x: 0, y: 0, w: 300, h: 20) : $0.element })
    }

    func testTheCarriedTileRidesUnderThePointerAndOthersBlend() {
        let order = [0, 1, 2, 3]
        let here = TileMotion.stop(order: order, dragged: 3, slot: 3, metrics: m)
        let up = TileMotion.stop(order: order, dragged: 3, slot: 2, metrics: m)
        let between = CGPoint(x: (here.x + up.x) / 2, y: (here.y + up.y) / 2)
        let frame = TileMotion.frame(order: order, dragged: 3, anchor: between, metrics: m)
        XCTAssertEqual(frame.boxes[3].origin, between, "1:1 under the pointer")
        let rest = TileMotion.view(order: order, metrics: m)
        let moved = TileMotion.view(order: TileMotion.order(order, moving: 3, to: 2), metrics: m)
        for block in [0, 1, 2] {
            let lo = min(rest[block].y, moved[block].y), hi = max(rest[block].y, moved[block].y)
            XCTAssertGreaterThanOrEqual(frame.boxes[block].y, lo - 0.001)
            XCTAssertLessThanOrEqual(frame.boxes[block].y, hi + 0.001)
        }
    }

    func testLegalSlotsLimitTheStatesTheDragCanReach() {
        let frame = TileMotion.frame(order: [0, 1, 2, 3], dragged: 0, anchor: CGPoint(x: 0, y: 500), metrics: m, legal: [0, 3])
        XCTAssertEqual(frame.nearest, 3)
    }
}

// MARK: panel.json

final class PanelPrefsTests: XCTestCase {
    func testNothingSavedReadsAsTheDefaults() {
        let store = PanelPrefsStore(url: tempFile())
        let prefs = store.load()
        XCTAssertEqual(prefs.show, .used)
        XCTAssertEqual(prefs.tabs, ["overview", "claude", "codex", "copilot", "agy", "go", "openrouter-free", "gemini-metered", "chatgpt-work", "other"])
        XCTAssertTrue(prefs.hiddenTabs.isEmpty)
        XCTAssertEqual(prefs.tileOrder("claude", available: PanelPrefs.providerTiles), PanelPrefs.providerTiles)
        XCTAssertEqual(prefs.tileOrder("overview", available: PanelPrefs.overviewTiles), PanelPrefs.overviewTiles)
    }

    func testTheFileIsMode600WrittenAtomicallyAndKeepsUnknownKeys() throws {
        let url = tempFile()
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(#"{"schema":1,"future":{"a":[1,2]},"show":"left","tabs":["overview","codex"],"tiles":{"claude":["links"]},"mystery":true}"#.utf8).write(to: url)
        let store = PanelPrefsStore(url: url)
        var prefs = store.load()
        XCTAssertEqual(prefs.show, .left)
        XCTAssertEqual(Array(prefs.tabs.prefix(2)), ["overview", "codex"], "absent ids are appended")
        XCTAssertTrue(prefs.tabs.contains("claude"))
        prefs.show = .used
        prefs.toggleTab("copilot")
        XCTAssertTrue(store.save(prefs))
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        XCTAssertEqual((attributes[.posixPermissions] as? NSNumber)?.intValue, 0o600)
        let raw = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        XCTAssertEqual(raw["mystery"] as? Bool, true)
        XCTAssertNotNil(raw["future"])
        XCTAssertEqual(raw["show"] as? String, "used")
        XCTAssertEqual(raw["hidden_tabs"] as? [String], ["copilot"])
        XCTAssertEqual(raw["schema"] as? Int, 1)
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: url.deletingLastPathComponent().path).filter { $0.hasSuffix(".tmp") }
        XCTAssertTrue(leftovers.isEmpty, "the temporary file is renamed over the real one")
    }

    func testAKeyAnotherWriterAddedBetweenLoadAndSaveSurvives() throws {
        let url = tempFile()
        let store = PanelPrefsStore(url: url)
        var prefs = store.load()
        prefs.show = .left
        XCTAssertTrue(store.save(prefs))
        var raw = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        raw["web_only"] = ["x": 1]
        try JSONSerialization.data(withJSONObject: raw).write(to: url)
        prefs.show = .used
        XCTAssertTrue(store.save(prefs))
        raw = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        XCTAssertNotNil(raw["web_only"])
    }

    func testTheContractsTabIdsMapToTheMenusAndBack() throws {
        let url = tempFile()
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(#"{"tabs":["overview","antigravity","opencode","claude"],"hidden_tabs":["opencode"],"tiles":{"antigravity":["policy","limits"]}}"#.utf8).write(to: url)
        let store = PanelPrefsStore(url: url)
        let prefs = store.load()
        XCTAssertEqual(Array(prefs.tabs.prefix(4)), ["overview", "agy", "go", "claude"])
        XCTAssertEqual(prefs.hiddenTabs, ["go"])
        XCTAssertEqual(prefs.tiles["agy"], ["policy", "limits"])
        XCTAssertTrue(store.save(prefs))
        let raw = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        XCTAssertEqual((raw["tabs"] as? [String])?.prefix(4).map { $0 }, ["overview", "antigravity", "opencode", "claude"])
        XCTAssertEqual(raw["hidden_tabs"] as? [String], ["opencode"])
    }

    func testTheEnvironmentChoosesTheFile() {
        let home = URL(fileURLWithPath: "/home/someone")
        XCTAssertEqual(PanelPrefsStore.defaultURL(environment: [:], home: home).path, "/home/someone/.config/orchestrator/panel.json")
        XCTAssertEqual(PanelPrefsStore.defaultURL(environment: ["XDG_CONFIG_HOME": "/x"], home: home).path, "/x/orchestrator/panel.json")
        XCTAssertEqual(PanelPrefsStore.defaultURL(environment: ["XDG_CONFIG_HOME": "/x", "FLEET_PANEL_PREFS": "/y/p.json"], home: home).path, "/y/p.json")
        XCTAssertEqual(PanelPrefsStore.defaultURL(environment: ["FLEET_PANEL_PREFS": "relative.json"], home: home).path, "/home/someone/.config/orchestrator/panel.json", "a relative override is ignored")
    }

    func testTabsKeepOtherLastAndMoveOntoEachOther() {
        var prefs = PanelPrefs()
        let all = ["claude", "codex", "copilot", "other"]
        XCTAssertEqual(prefs.orderedTabs(all), all)
        prefs.tabs = ["overview", "other", "copilot", "claude"]
        XCTAssertEqual(prefs.orderedTabs(all), ["copilot", "claude", "codex", "other"])
        XCTAssertFalse(prefs.moveTab("other", onto: "claude", among: all))
        XCTAssertTrue(prefs.moveTab("codex", onto: "copilot", among: all))
        XCTAssertEqual(prefs.orderedTabs(all), ["codex", "copilot", "claude", "other"])
        XCTAssertTrue(prefs.isTabVisible("overview"))
        prefs.toggleTab("overview")
        XCTAssertTrue(prefs.isTabVisible("overview"), "Overview cannot be hidden")
        prefs.toggleTab("claude")
        XCTAssertFalse(prefs.isTabVisible("claude"))
    }

    func testTilesShowInTheSavedOrderHiddenOnesDropAndPluginsOnlyWhereAdded() {
        var prefs = PanelPrefs()
        let available = PanelPrefs.providerTiles + ["plugin:gh/runs"]
        XCTAssertFalse(prefs.tileOrder("claude", available: available).contains("plugin:gh/runs"), "a plugin tile is not shown until it is added")
        prefs.setTile("claude", "credits", on: false)
        XCTAssertEqual(prefs.hiddenTiles["claude"], ["credits"])
        XCTAssertFalse(prefs.tileOrder("claude", available: available).contains("credits"))
        prefs.setTile("claude", "plugin:gh/runs", on: true)
        XCTAssertEqual(prefs.tileOrder("claude", available: available).last, "plugin:gh/runs")
        prefs.setTileOrder("claude", shown: ["links", "limits", "plugin:gh/runs"])
        XCTAssertEqual(Array(prefs.tileOrder("claude", available: available).prefix(3)), ["links", "limits", "plugin:gh/runs"])
        prefs.setTile("claude", "plugin:gh/runs", on: false)
        XCTAssertFalse(prefs.tileOrder("claude", available: available).contains("plugin:gh/runs"))
        prefs.setTile("claude", "credits", on: true)
        XCTAssertNil(prefs.hiddenTiles["claude"])
    }

    func testATileThatIsNotThereNowKeepsItsSlot() {
        var prefs = PanelPrefs()
        prefs.setTileOrder("claude", shown: ["links", "limits"])   // resets, tokens, policy are not drawn now
        let order = prefs.tiles["claude"]!
        XCTAssertEqual(Array(order.prefix(2)), ["links", "limits"])
        XCTAssertTrue(order.contains("resets") && order.contains("policy"))
    }

    func testOverviewOrderBecomesTheTabOrderAndTheGroupKeepsItsPlace() {
        var prefs = PanelPrefs()
        let providers = ["claude", "codex", "copilot", "agy", "go"]
        prefs.setOverviewOrder(["provider:codex", "provider:claude", "provider:copilot", "provider:agy", "provider:go", "subscriptions", "tokens", "links"], providers: providers)
        XCTAssertEqual(prefs.orderedTabs(providers), ["codex", "claude", "copilot", "agy", "go"])
        XCTAssertEqual(Array(prefs.tiles["overview"]!.prefix(4)), ["providers", "subscriptions", "tokens", "links"])
        prefs.setOverviewOrder(["subscriptions", "provider:codex", "provider:claude", "provider:copilot", "provider:agy", "provider:go", "tokens", "links"], providers: providers)
        XCTAssertEqual(Array(prefs.tiles["overview"]!.prefix(4)), ["subscriptions", "providers", "tokens", "links"])
    }

    func testTheTabOrderTheHiddenTabsAndClaudesBlocksMoveOverOnceFromUserDefaults() throws {
        let name = "org.crossfeed.test.r11." + UUID().uuidString
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        defaults.set(["codex", "claude"], forKey: "crossfeed.provider-order")
        defaults.set(["copilot"], forKey: "crossfeed.tabs.hidden")
        defaults.set(["policy", "row:claude:weekly", "status"], forKey: "sectionOrder.claude")
        let store = PanelPrefsStore(url: tempFile())
        XCTAssertTrue(PanelMigration.run(store: store, defaults: defaults))
        let prefs = store.load()
        XCTAssertEqual(Array(prefs.orderedTabs(["claude", "codex", "copilot"]).prefix(2)), ["codex", "claude"])
        XCTAssertEqual(prefs.hiddenTabs, ["copilot"])
        XCTAssertEqual(Array(prefs.tiles["claude"]!.prefix(2)), ["policy", "limits"])
        XCTAssertNil(defaults.stringArray(forKey: "crossfeed.provider-order"), "the old keys are gone")
        XCTAssertNil(defaults.stringArray(forKey: "sectionOrder.claude"))
        // Once: a later change in the file is not undone by a second run, and nothing is read from UserDefaults again.
        var changed = prefs
        changed.hiddenTabs = []
        XCTAssertTrue(store.save(changed))
        defaults.set(["copilot"], forKey: "crossfeed.tabs.hidden")
        XCTAssertFalse(PanelMigration.run(store: store, defaults: defaults))
        XCTAssertEqual(store.load().hiddenTabs, [])
    }

    func testAFileThatAlreadyHasATabOrderWinsOverTheOldPreference() throws {
        let name = "org.crossfeed.test.r11b." + UUID().uuidString
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        let store = PanelPrefsStore(url: tempFile())
        var prefs = PanelPrefs()
        prefs.tabs = ["overview", "agy", "claude", "codex", "copilot", "go", "other"]
        XCTAssertTrue(store.save(prefs))
        defaults.set(["codex", "claude"], forKey: "crossfeed.provider-order")
        PanelMigration.run(store: store, defaults: defaults)
        XCTAssertEqual(store.load().tabs.prefix(3).map { $0 }, ["overview", "agy", "claude"])
    }
}

// MARK: The glance's round 11 parts and what the tiles say

final class TileModelTests: XCTestCase {
    func testTheGlanceCarriesTokensResetsAndTheUsdTotal() throws {
        let glance = try Glance.decode(fixtureData("glance-round11.json"))
        let claude = glance.pools.first { $0.pool == "claude" }!
        XCTAssertEqual(claude.tokens?.today, 958_000_542)
        XCTAssertEqual(claude.tokens?.days.count, 14)
        XCTAssertEqual(claude.limitResets.map(\.key), ["weekly", "claude-weekly-scoped-fable"])
        XCTAssertEqual(claude.limitResets.map(\.next), [true, false])
        XCTAssertEqual(glance.spend?.usd?.operatorAmount, "348.35")
        XCTAssertEqual(glance.spend?.plans.first { $0.id == "codex" }?.share, "1/2")
        XCTAssertTrue(glance.plugins.isEmpty)
        XCTAssertNil(glance.pools.first { $0.pool == "github-copilot-student" }?.tokens)
    }

    func testOverviewSaysWhatRunsOutFirstAndShowsOneTileEachWithTheTightestWindow() throws {
        let p = try presenter()
        let page = p.overviewPage(order: ["claude", "codex", "copilot", "agy", "go"], show: .used)
        XCTAssertEqual(page.alert, OverviewPage.Alert(text: "Gemini runs out in 1h 52m", hot: true, tab: "agy"))
        XCTAssertEqual(page.providers.map(\.id), ["claude", "codex", "copilot", "agy", "go"])
        let claude = page.providers[0]
        XCTAssertEqual(claude.line?.figure, "58% used")
        XCTAssertEqual(claude.window, "Weekly")
        XCTAssertEqual(claude.line?.resets, "resets Tue 14:00")
        XCTAssertEqual(claude.line?.pace, PaceWord(text: "17% deficit", kind: .deficit))
        XCTAssertEqual(claude.tickets, 2)
        XCTAssertEqual(page.providers[1].line?.figure, "Used up")
        XCTAssertEqual(page.providers[1].window, "Weekly")
        XCTAssertEqual(page.providers[2].line?.pace, PaceWord(text: "88% surplus", kind: .surplus))
        XCTAssertEqual(page.providers[2].window, "Monthly", "Copilot's 'Premium monthly' reads as Monthly")
        XCTAssertEqual(page.providers[3].window, "Gemini weekly")
        XCTAssertEqual(page.providers[3].line?.figure, "93% used")
        XCTAssertEqual(page.providers[4].line?.pace, PaceWord(text: "15% surplus", kind: .surplus))
        XCTAssertEqual(page.links.map(\.title), ["Usage and cost"], "the footer already opens the console")
    }

    func testSubscriptionsShowTheShareAndTheYouPayLineFromTheUsdOperatorTotal() throws {
        let model = try presenter().overviewPage(order: [], show: .used).subscriptions!
        XCTAssertEqual(model.rows.map(\.name), ["Claude Max 20x", "ChatGPT Pro", "OpenCode Go", "Google AI Pro"])
        XCTAssertEqual(model.rows[1].share, "½")
        XCTAssertEqual(model.rows[1].full, "$200")
        XCTAssertEqual(model.rows[1].yours, "$100")
        XCTAssertNil(model.rows[0].share)
        XCTAssertEqual(model.rows[0].yours, "£183")
        XCTAssertEqual(model.total, "≈ $348")
        let none = try presenter { object in
            var spend = object["subscription_spend"] as! [String: Any]
            spend.removeValue(forKey: "usd")
            object["subscription_spend"] = spend
        }.overviewPage(order: [], show: .used).subscriptions!
        XCTAssertNil(none.total, "no USD total from fleetctl: no total is made up")
    }

    func testOverviewTokensSumEveryProviderPerDay() throws {
        let tokens = try presenter().overviewPage(order: [], show: .used).tokens!
        XCTAssertEqual(tokens.today, "958M")
        XCTAssertEqual(tokens.days.count, 14)
        XCTAssertEqual(tokens.days.last, 958_000_542)
        XCTAssertEqual(Presenter.big(0), "0")
        XCTAssertEqual(Presenter.big(12_345), "12k")
        XCTAssertEqual(Presenter.big(1_200_000_000), "1.2B")
        XCTAssertEqual(Presenter.big(15_000_000_000), "15B")
    }

    func testClaudeShowsOnlyWindowsItReportsWithTheWindowNamedAndPace() throws {
        let page = try presenter().providerPage(for: "claude", show: .used)!
        let lines = page.groups.flatMap(\.lines)
        XCTAssertEqual(lines.map(\.window), ["Weekly", "Fable weekly"], "the idle 5-hour placeholder is not a row")
        XCTAssertEqual(lines[0].figure, "58% used")
        XCTAssertEqual(lines[0].runsOut, "Runs out in 2d 1h")
        XCTAssertEqual(lines[0].resets, "resets Tue 14:00")
        XCTAssertTrue(lines[0].hot)
        XCTAssertEqual(lines[1].pace, PaceWord(text: "25% surplus", kind: .surplus))
        XCTAssertFalse(lines[1].hot)
        XCTAssertEqual(page.plan, "Max 20x")
        XCTAssertEqual(page.title, "Claude")
        XCTAssertEqual(page.updated, "Updated 4 min ago")
        XCTAssertEqual(page.incident?.word, "Minor issue")
        XCTAssertFalse(page.incident?.severe ?? true)
        XCTAssertEqual(page.tokens?.today, "958M")
        XCTAssertNotNil(page.policy)
        XCTAssertEqual(page.available, ["limits", "resets", "tokens", "policy", "status", "links"])
    }

    func testAFiveHourRowAppearsOnlyWhenTheProviderReportsOne() throws {
        let p = try presenter { object in
            var pools = object["pools"] as! [[String: Any]]
            let i = pools.firstIndex { $0["pool"] as? String == "claude" }!
            var limits = pools[i]["limits"] as! [[String: Any]]
            limits[0] = ["kind": "window", "minutes": 300, "name": "session", "title": "5-hour", "used_percent": 40,
                         "reset_at": "2026-11-27T15:20:00Z", "pace": ["elapsed_percent": 33.3, "status": "deficit", "runs_out_in_s": 5520]]
            pools[i]["limits"] = limits
            object["pools"] = pools
        }
        let lines = p.providerPage(for: "claude", show: .used)!.groups.flatMap(\.lines)
        XCTAssertEqual(lines.map(\.window), ["5-hour", "Weekly", "Fable weekly"])
        XCTAssertEqual(lines[0].runsOut, "Runs out in 1h 32m")
    }

    func testClaudePlanComesFromTheOverlayNameSoUltraShowsUltra() throws {
        let p = try presenter { object in
            var spend = object["subscription_spend"] as! [String: Any]
            var plans = spend["plans"] as! [[String: Any]]
            plans[0]["name"] = "Claude Ultra"
            spend["plans"] = plans
            object["subscription_spend"] = spend
        }
        XCTAssertEqual(p.providerPage(for: "claude", show: .used)?.plan, "Ultra")
    }

    func testResetsNameWhatTheyClearAndOnlyClaudesCanBeUsed() throws {
        let p = try presenter()
        let claude = p.providerPage(for: "claude", show: .used)!.resets
        XCTAssertEqual(claude.map(\.name), ["Weekly", "Fable"])
        XCTAssertEqual(claude.map(\.next), [true, false])
        XCTAssertEqual(claude.map(\.until), ["10 Dec", "9 Mar"])
        XCTAssertEqual(claude.map(\.canUse), [true, true])
        XCTAssertEqual(claude.map(\.key), ["weekly", "claude-weekly-scoped-fable"])
        XCTAssertEqual(claude[0].ask, "Use the weekly reset?")
        XCTAssertEqual(claude[0].effect, "Weekly 58% → 0% · Fable 16% → 0%")
        XCTAssertEqual(claude[1].ask, "Use the Fable reset?")
        XCTAssertEqual(claude[1].effect, "Fable 16% → 0%")
        let codex = p.providerPage(for: "codex", show: .used)!.resets
        XCTAssertEqual(codex.map(\.name), ["Limit", "Limit"])
        XCTAssertEqual(codex.map(\.canUse), [false, false], "fleetctl uses Claude's only")
    }

    func testCodexIsUsedUpWithCreditsFromItsBillingGrant() throws {
        let page = try presenter().providerPage(for: "codex", show: .used)!
        XCTAssertEqual(page.badge, HeaderBadge(word: "Used up", hot: true))
        XCTAssertEqual(page.groups.flatMap(\.lines).first?.figure, "Used up")
        XCTAssertEqual(page.groups.flatMap(\.lines).first?.bar, PaceBarModel(segments: [BarSegment(from: 0, to: 100, kind: .deficit)], tick: nil))
        XCTAssertEqual(page.credits, CreditsModel(amount: "11,872 credits", until: "until 1 Jan"))
        XCTAssertEqual(page.plan, "Pro")
        XCTAssertEqual(page.title, "Codex")
    }

    func testAntigravityListsItsAllowancesAndSaysArgonSharesGeminis() throws {
        let page = try presenter().providerPage(for: "agy", show: .used)!
        XCTAssertEqual(page.groups.map { $0.title ?? "" }, ["Gemini", "Claude and GPT", "Argon"])
        XCTAssertEqual(page.groups[0].lines.map(\.window), ["5-hour", "Weekly"])
        XCTAssertEqual(page.groups[2].lines.count, 0)
        XCTAssertEqual(page.groups[2].note, "Shares the Gemini 5-hour limit")
        XCTAssertEqual(page.groups[1].lines.map(\.resets), ["reset time unknown", "reset time unknown"])
        XCTAssertEqual(page.title, "Antigravity")
        XCTAssertEqual(page.plan, "Google AI Pro")
        XCTAssertEqual(page.badge?.word, "Low")
    }

    func testTheLinksAreUsageAndStatusOnEveryProviderTabWithNoConsoleAndNoStatusOnOverview() throws {
        let p = try presenter()
        let claude = p.providerPage(for: "claude", show: .used)!.links
        XCTAssertEqual(claude.map(\.title), ["Usage and cost", "Status"])
        XCTAssertEqual(claude[0].kind, .usage("claude"))
        XCTAssertEqual(claude[1].kind, .status(URL(string: "https://status.claude.com/")!))
        XCTAssertEqual(p.providerPage(for: "agy", show: .used)!.links[0].kind, .usage("antigravity"))
        XCTAssertFalse(p.overviewPage(order: [], show: .used).links.contains { $0.title == "Status" })
    }

    func testLeftModeSaysWhatIsLeftAndFlipsTheBar() throws {
        let line = try presenter().providerPage(for: "claude", show: .left)!.groups[0].lines[0]
        XCTAssertEqual(line.figure, "42% left")
        XCTAssertEqual(line.bar.segments.first, BarSegment(from: 0, to: 42, kind: .fill))
        XCTAssertEqual(line.bar.segments.last?.kind, .deficit)
        XCTAssertEqual(line.bar.tick ?? 0, 59, accuracy: 0.5)
        XCTAssertEqual(line.pace?.text, "17% deficit", "the pace word does not change with the mode")
    }

    func testTheBarsSurplusRunsFromTheFillsEndExactlyToTheTickAndDeficitFromTheTickToTheFillsEnd() {
        let surplus = PaceBarModel.make(used: 16, elapsed: 41.1, show: .used, full: false)
        XCTAssertEqual(surplus.segments, [BarSegment(from: 0, to: 16, kind: .fill), BarSegment(from: 16, to: 41.1, kind: .surplus)])
        XCTAssertEqual(surplus.tick, 41.1)
        let deficit = PaceBarModel.make(used: 58, elapsed: 41.1, show: .used, full: false)
        XCTAssertEqual(deficit.segments, [BarSegment(from: 0, to: 41.1, kind: .fill), BarSegment(from: 41.1, to: 58, kind: .deficit)])
        let noPace = PaceBarModel.make(used: 30, elapsed: nil, show: .used, full: false)
        XCTAssertEqual(noPace.segments, [BarSegment(from: 0, to: 30, kind: .fill)])
        XCTAssertNil(noPace.tick)
        let leftSurplus = PaceBarModel.make(used: 16, elapsed: 41.1, show: .left, full: false)
        XCTAssertEqual(leftSurplus.segments.last?.kind, .surplus)
        XCTAssertEqual(leftSurplus.segments.map(\.to).last ?? 0, 84, accuracy: 0.001)
        for model in [surplus, deficit, leftSurplus] {
            for pair in zip(model.segments, model.segments.dropFirst()) { XCTAssertEqual(pair.0.to, pair.1.from, "flush, no gaps") }
        }
    }

    func testOtherListsWhatIsNotMetered() throws {
        // A pool no tab knows by name and that is billed per nothing: Other lists it as not metered.
        let page = try presenter(edit: { object in
            var pools = object["pools"] as! [[String: Any]]
            pools[6]["pool"] = "mystery-metered"
            object["pools"] = pools
        }).providerPage(for: "other", show: .used)!
        XCTAssertFalse(page.notMetered.isEmpty)
        XCTAssertNil(page.plan)
        XCTAssertEqual(page.updated, "Not metered")
    }

    func testWhenTextIsTheTimeTodayTheWeekdayWithinAWeekElseTheDay() throws {
        let p = try presenter()
        XCTAssertEqual(p.whenText(now.addingTimeInterval(3 * 3600)), "14:02")
        XCTAssertEqual(p.whenText(now.addingTimeInterval(2 * 86400)), "Sun 11:02")
        XCTAssertEqual(p.whenText(now.addingTimeInterval(12 * 86400)), "9 Dec")
    }
}

// MARK: Plugin tiles and using a reset

final class PluginAndResetTests: XCTestCase {
    private func entry(_ doc: String) throws -> PluginEntry {
        let json = #"{"plugin":"gh-actions","tile":"runs","tab":"overview","doc":\#(doc),"error":null}"#
        return try JSONDecoder().decode(PluginEntry.self, from: Data(json.utf8))
    }

    func testTheContractsExampleDocumentDecodesIntoTheSameRowKinds() throws {
        let glance = try Glance.decode(fixtureData("glance-round11-plugins.json"))
        let tile = try XCTUnwrap(glance.plugins.first)
        XCTAssertEqual(tile.id, "plugin:gh-actions/runs")
        XCTAssertEqual(tile.doc.title, "CI runs")
        XCTAssertEqual(tile.doc.status, .ok)
        XCTAssertEqual(tile.doc.rows.count, 5)
        guard case .bar(let used, let pace, let window, _) = tile.doc.rows[0].kind else { return XCTFail("bar") }
        XCTAssertEqual([used, pace ?? -1], [58, 41])
        XCTAssertEqual(window, "Monthly")
        guard case .value(let value, let tone) = tile.doc.rows[1].kind else { return XCTFail("value") }
        XCTAssertEqual([value, tone.rawValue], ["3", "warn"])
        guard case .sparkline(let values, let today) = tile.doc.rows[2].kind else { return XCTFail("sparkline") }
        XCTAssertEqual(values, [3, 5, 2, 8]); XCTAssertEqual(today, "8")
        guard case .link(let url) = tile.doc.rows[4].kind else { return XCTFail("link") }
        XCTAssertEqual(url.scheme, "https")
    }

    func testAPluginTileAppearsOnItsTabOnlyWhenTheOperatorAddedIt() throws {
        let p = try presenter("glance-round11-plugins.json")
        var prefs = PanelPrefs()
        let available = [TileID.alert, TileID.providers, TileID.links] + p.glance.plugins.map(\.id)
        XCTAssertFalse(prefs.tileOrder("overview", available: available).contains("plugin:gh-actions/runs"))
        prefs.setTile("overview", "plugin:gh-actions/runs", on: true)
        XCTAssertTrue(prefs.tileOrder("overview", available: available).contains("plugin:gh-actions/runs"))
    }

    func testAnInvalidDocumentBecomesAnErrorTileWithThePlainSentence() throws {
        let tooMany = try entry(#"{"title":"x","status":"ok","rows":[\#((0..<9).map { _ in #"{"kind":"text","text":"a"}"# }.joined(separator: ","))]}"#)
        XCTAssertEqual(tooMany.doc.status, .error)
        XCTAssertEqual(tooMany.doc.rows.map(\.label), ["Plugin output invalid"])
        let http = try entry(#"{"title":"x","status":"ok","rows":[{"kind":"link","label":"Open","url":"http://example.com"}]}"#)
        XCTAssertEqual(http.doc.status, .error, "links are https only")
        let long = try entry(#"{"title":"x","status":"ok","rows":[{"kind":"text","text":"\#(String(repeating: "a", count: 121))"}]}"#)
        XCTAssertEqual(long.doc.rows.map(\.label), ["Plugin output invalid"])
        let range = try entry(#"{"title":"x","status":"ok","rows":[{"kind":"bar","label":"m","used":101}]}"#)
        XCTAssertEqual(range.doc.status, .error)
        let notAnObject = try entry(#""nope""#)
        XCTAssertEqual(notAnObject.doc.status, .error)
    }

    func testUnknownRowKindsAreDroppedAndAnErrorFromTheGlanceIsShownInWords() throws {
        let mixed = try entry(#"{"title":"x","status":"warn","rows":[{"kind":"hologram","label":"?"},{"kind":"text","text":"ok"}]}"#)
        XCTAssertEqual(mixed.doc.status, .warn)
        XCTAssertEqual(mixed.doc.rows.map(\.label), ["ok"])
        let failed = try JSONDecoder().decode(PluginEntry.self, from: Data(#"{"plugin":"p","tile":"t","doc":null,"error":"timed out"}"#.utf8))
        XCTAssertEqual(failed.doc.status, .error)
        XCTAssertEqual(failed.doc.rows.map(\.label), ["timed out"])
        XCTAssertEqual(failed.tab, "overview")
    }

    func testResetOutcomesAreReadFromFleetctlsJson() {
        XCTAssertEqual(ResetOutcome.parse(Data(#"{"ok":true,"left":0,"label":"x","sent":true,"error":null}"#.utf8), exitCode: 0), .used(left: 0))
        XCTAssertEqual(ResetOutcome.parse(Data(#"{"ok":true,"left":3}"#.utf8), exitCode: 0), .used(left: 3))
        XCTAssertEqual(ResetOutcome.parse(Data(#"{"ok":false,"error":"Anthropic did not accept the reset (HTTP 429)"}"#.utf8), exitCode: 1),
                       .failed("Anthropic did not accept the reset (HTTP 429)"))
        XCTAssertEqual(ResetOutcome.parse(Data("not json".utf8), exitCode: 1), .failed("Not used"))
        XCTAssertEqual(ResetOutcome.parse(Data(), exitCode: 0), .failed("No answer"))
    }
}
