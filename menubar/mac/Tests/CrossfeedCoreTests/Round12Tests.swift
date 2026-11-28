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

private func presenter(_ panel: PanelPrefs = PanelPrefs(), edit: ((inout [String: Any]) -> Void)? = nil) throws -> Presenter {
    var data = try fixtureData("glance-round12.json")
    if let edit {
        var object = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        edit(&object)
        data = try JSONSerialization.data(withJSONObject: object)
    }
    var p = Presenter(glance: try Glance.decode(data), loadedAt: now, now: now, timeZone: rome)
    p.panel = panel
    return p
}

private func tempFile() -> URL {
    FileManager.default.temporaryDirectory.appendingPathComponent("crossfeed-r12-\(UUID().uuidString)").appendingPathComponent("panel.json")
}

/// Edits one pool's `api` block in the fixture.
private func editAPI(_ pool: String, _ change: @escaping (inout [String: Any]) -> Void) -> (inout [String: Any]) -> Void {
    { object in
        var pools = object["pools"] as! [[String: Any]]
        for i in pools.indices where pools[i]["pool"] as? String == pool {
            var api = (pools[i]["api"] as? [String: Any]) ?? [:]
            change(&api)
            pools[i]["api"] = api
        }
        object["pools"] = pools
    }
}

// MARK: panel.json: the new keys

final class Round12PanelTests: XCTestCase {
    func testTheNewKeysReadAsTheContractsDefaultsWhenNothingIsSaved() {
        let prefs = PanelPrefsStore(url: tempFile()).load()
        XCTAssertEqual(prefs.appearance, .system)
        XCTAssertNil(prefs.menubarIcon)
        XCTAssertTrue(prefs.refreshOnOpen)
        XCTAssertEqual(prefs.refreshEveryMinutes, 5)
        XCTAssertEqual(prefs.openOn, "last")
        XCTAssertNil(prefs.lastTab)
        XCTAssertEqual(prefs.budgets, [:])
        XCTAssertEqual(prefs.tileOptions, [:])
    }

    func testEverySettingSurvivesARestart() throws {
        let url = tempFile()
        let store = PanelPrefsStore(url: url)
        var prefs = PanelPrefs()
        prefs.appearance = .dark
        prefs.menubarIcon = "provider"
        prefs.refreshOnOpen = false
        prefs.refreshEveryMinutes = 15
        prefs.openOn = "agy"
        prefs.lastTab = "go"
        prefs.show = .left
        prefs.budgets = ["openrouter-free": 20, "gemini-metered": 12.5]
        prefs.setOption("agy", "tokens") { $0.metric = .tokens; $0.days = 30 }
        prefs.setOption("openrouter-free", "activity") { $0.metric = .cost; $0.days = 7 }
        prefs.setTile("claude", "status", on: false)
        prefs.toggleTab("copilot")
        XCTAssertTrue(store.save(prefs))
        // A new store on the same file is what the next launch is.
        let back = PanelPrefsStore(url: url).load()
        XCTAssertEqual(back, prefs)
        let raw = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        XCTAssertEqual(raw["appearance"] as? String, "dark")
        XCTAssertEqual((raw["menubar"] as? [String: Any])?["icon"] as? String, "provider")
        XCTAssertEqual((raw["refresh"] as? [String: Any])?["on_open"] as? Bool, false)
        XCTAssertEqual((raw["refresh"] as? [String: Any])?["every_minutes"] as? Int, 15)
        XCTAssertEqual(raw["open_on"] as? String, "antigravity", "the file uses the contract's tab ids")
        XCTAssertEqual(raw["last_tab"] as? String, "opencode")
        XCTAssertEqual((raw["budgets"] as? [String: Any])?["openrouter-free"] as? Int, 20)
        let options = raw["tile_options"] as? [String: [String: Any]]
        XCTAssertEqual(options?["antigravity/tokens"]?["days"] as? Int, 30)
        XCTAssertEqual(options?["openrouter-free/activity"]?["metric"] as? String, "cost")
    }

    func testBadValuesFallBackToTheDefaultsAndKeysOfOtherWritersSurvive() throws {
        let url = tempFile()
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(#"""
        {"appearance":"sepia","menubar":{"icon":"hologram","web_only":7},"refresh":{"on_open":"yes","every_minutes":-4,"jitter":2},
         "open_on":"","last_tab":5,"budgets":{"a":-3,"b":"lots","c":15},"tile_options":{"claude/tokens":{"metric":"mana","days":99},"x":{"days":7}},
         "web_only":true}
        """#.utf8).write(to: url)
        let store = PanelPrefsStore(url: url)
        let prefs = store.load()
        XCTAssertEqual(prefs.appearance, .system)
        XCTAssertNil(prefs.menubarIcon)
        XCTAssertTrue(prefs.refreshOnOpen)
        XCTAssertEqual(prefs.refreshEveryMinutes, 5)
        XCTAssertEqual(prefs.openOn, "last")
        XCTAssertNil(prefs.lastTab)
        XCTAssertEqual(prefs.budgets, ["c": 15])
        XCTAssertEqual(prefs.tileOptions, [:])
        var changed = prefs
        changed.menubarIcon = "none"
        XCTAssertTrue(store.save(changed))
        let raw = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        XCTAssertEqual((raw["menubar"] as? [String: Any])?["web_only"] as? Int, 7, "an unknown key inside menubar is kept")
        XCTAssertEqual((raw["refresh"] as? [String: Any])?["jitter"] as? Int, 2)
        XCTAssertEqual(raw["web_only"] as? Bool, true)
        var cleared = changed
        cleared.menubarIcon = nil
        XCTAssertTrue(store.save(cleared))
        let again = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        XCTAssertNil((again["menubar"] as? [String: Any])?["icon"], "Reset takes the icon choice out of the file")
    }

    func testTheIconChoiceMapsToTheFilesWords() {
        XCTAssertEqual(BarPrefs.Icon.tanks.fileValue, "crossfeed")
        XCTAssertEqual(BarPrefs.Icon.logo.fileValue, "provider")
        XCTAssertEqual(BarPrefs.Icon.none.fileValue, "none")
        for icon in BarPrefs.Icon.allCases { XCTAssertEqual(BarPrefs.Icon(file: icon.fileValue), icon) }
        XCTAssertNil(BarPrefs.Icon(file: "logo"))
    }

    func testTheMenuOpensOnTheLastTabOrTheOneChosenAndFallsBackToOverview() {
        var prefs = PanelPrefs()
        let shown = ["overview", "claude", "codex"]
        XCTAssertEqual(prefs.startTab(shown: shown), "overview", "nothing used yet")
        prefs.lastTab = "codex"
        XCTAssertEqual(prefs.startTab(shown: shown), "codex", "Last used")
        prefs.openOn = "claude"
        XCTAssertEqual(prefs.startTab(shown: shown), "claude", "the tab chosen wins over the last one")
        prefs.openOn = "overview"
        XCTAssertEqual(prefs.startTab(shown: shown), "overview", "Overview can be chosen")
        prefs.openOn = "last"
        XCTAssertEqual(prefs.startTab(shown: ["overview", "claude"]), "overview", "a hidden last tab falls back")
        prefs.openOn = "copilot"
        XCTAssertEqual(prefs.startTab(shown: shown), "overview", "a tab that is gone falls back")
    }

    func testTheOldMenuBarRefreshChoiceMovesIntoTheFileOnce() throws {
        let name = "org.crossfeed.test.r12.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        defaults.set(900, forKey: "crossfeed.bar.refresh")
        let store = PanelPrefsStore(url: tempFile())
        XCTAssertTrue(PanelMigration.moveRefresh(store: store, defaults: defaults))
        XCTAssertEqual(store.load().refreshEveryMinutes, 15)
        XCTAssertNil(defaults.object(forKey: "crossfeed.bar.refresh"))
        var prefs = store.load()
        prefs.refreshEveryMinutes = 1
        XCTAssertTrue(store.save(prefs))
        defaults.set(900, forKey: "crossfeed.bar.refresh")
        XCTAssertFalse(PanelMigration.moveRefresh(store: store, defaults: defaults), "once")
        XCTAssertEqual(store.load().refreshEveryMinutes, 1)
        // A file that already says how often wins over the old preference.
        let name2 = "org.crossfeed.test.r12b.\(UUID().uuidString)"
        let other = UserDefaults(suiteName: name2)!
        defer { other.removePersistentDomain(forName: name2) }
        other.set(60, forKey: "crossfeed.bar.refresh")
        XCTAssertFalse(PanelMigration.moveRefresh(store: store, defaults: other))
        XCTAssertEqual(store.load().refreshEveryMinutes, 1)
    }

    func testAnApiTabHasItsOwnTilesAndANewBuiltInTileLandsBeforeLinks() {
        var prefs = PanelPrefs()
        XCTAssertEqual(PanelPrefs.defaultTiles(for: "openrouter-free"), ["spend", "balance", "models", "activity", "status", "links"])
        XCTAssertEqual(PanelPrefs.defaultTiles(for: "claude").suffix(2), ["status", "links"])
        // A list saved before Status existed gets it before Links, not after.
        prefs.tiles["claude"] = ["limits", "resets", "tokens", "policy", "credits", "links"]
        let order = prefs.tileOrder("claude", available: PanelPrefs.providerTiles)
        XCTAssertEqual(order, ["limits", "resets", "tokens", "policy", "credits", "status", "links"])
        prefs.setTile("claude", "status", on: false)
        XCTAssertFalse(prefs.tileOrder("claude", available: PanelPrefs.providerTiles).contains("status"))
        // An unsaved tab shows every built-in tile it has, the operator's plugin tiles only where added.
        XCTAssertEqual(PanelPrefs().tileOrder("unknown-api", available: ["spend", "status", "links", "plugin:a/b"]), ["spend", "status", "links"])
    }

    func testOverviewStatusShowsByItselfAndStaysWhenPinnedAndHidesWhenTheOperatorSaysSo() {
        var prefs = PanelPrefs()
        let quiet = PanelPrefs.overviewTiles, loud = PanelPrefs.overviewTiles + ["status"]
        XCTAssertFalse(prefs.tileOrder("overview", available: quiet).contains("status"), "nothing to say: hidden")
        XCTAssertEqual(prefs.tileOrder("overview", available: loud, auto: ["status"]).suffix(2), ["status", "links"], "an incident: before Links")
        XCTAssertTrue(prefs.listed("overview", builtIns: loud, auto: ["status"]).on.contains("status"))
        prefs.setTile("overview", "status", on: false)
        XCTAssertFalse(prefs.tileOrder("overview", available: loud, auto: ["status"]).contains("status"), "hidden by the operator")
        prefs.setTile("overview", "status", on: true)
        XCTAssertTrue(prefs.tiles["overview"]!.contains("status"), "pinned")
        XCTAssertTrue(prefs.tileOrder("overview", available: loud).contains("status"))
    }

    func testChartOptionsDropWhenBackToTheDefault() {
        var prefs = PanelPrefs()
        prefs.setOption("claude", "tokens") { $0.days = 30 }
        XCTAssertEqual(prefs.option("claude", "tokens").days, 30)
        prefs.setOption("claude", "tokens") { $0.days = nil }
        XCTAssertEqual(prefs.tileOptions, [:])
    }
}

// MARK: The api block

final class Round12ApiTests: XCTestCase {
    func testTheContractsExampleDecodes() throws {
        let glance = try Glance.decode(fixtureData("glance-round12.json"))
        let api = try XCTUnwrap(glance.pools.first { $0.pool == "openrouter-free" }?.api)
        XCTAssertEqual(api.balanceUSD, 1.79)
        XCTAssertEqual(api.creditsUSD, 26)
        XCTAssertEqual(api.keyLimitUSD, 10)
        XCTAssertEqual(api.keyRemainingUSD, 3.39)
        XCTAssertEqual(api.spend, ApiSpend(todayUSD: 0, weekUSD: 0.09, monthUSD: 6.61))
        XCTAssertEqual(api.budgetUSD, 20)
        XCTAssertEqual(api.budgetSource, "operator")
        XCTAssertEqual(api.days.count, 30)
        XCTAssertEqual(api.days.last?.date, "2026-11-27")
        XCTAssertEqual(api.topModels.first, ApiModelSpend(model: "anthropic/claude-sonnet-5.5", usd: 3.1, requests: 221))
        XCTAssertEqual(api.source, "openrouter-api")
        XCTAssertEqual(api.sourceNote, "OpenRouter account")
    }

    func testEveryFieldIsOptionalAndAMalformedPartIsAbsentNotFatal() throws {
        let data = Data(#"{"schema":1,"pools":[{"pool":"openrouter-free","api":{"balance_usd":"lots","days":[{"date":"2026-11-27","usd":1.5},"x",{"usd":2}],"top_models":[{"usd":1},{"model":"a/b","usd":2}],"spend":{"month_usd":"x"},"billing_url":"http://insecure.example/","budget":{"monthly_usd":-5}}},{"pool":"gemini-metered","api":7},{"pool":"chatgpt-work","api":{}}]}"#.utf8)
        let glance = try Glance.decode(data)
        let api = try XCTUnwrap(glance.pools[0].api)
        XCTAssertNil(api.balanceUSD)
        XCTAssertEqual(api.days.map(\.date), ["2026-11-27", ""], "a day that is not an object is skipped")
        XCTAssertEqual(api.topModels.map(\.model), ["a/b"])
        XCTAssertNil(api.spend)
        XCTAssertNil(api.billingURL, "https only")
        XCTAssertNil(api.budgetUSD)
        XCTAssertNil(glance.pools[1].api)
        XCTAssertEqual(glance.pools[2].api?.days, [])
    }

    func testTheThreeApiPoolsEachGetATabAndOtherGoesAway() throws {
        let p = try presenter()
        XCTAssertEqual(p.tabs.map(\.id), ["claude", "codex", "copilot", "agy", "go", "openrouter-free", "gemini-metered", "chatgpt-work"])
        XCTAssertEqual(p.tabs.suffix(3).map(\.short), ["Router", "Gemini", "Chat"])
        XCTAssertTrue(p.isAPITab("openrouter-free"))
        XCTAssertFalse(p.isAPITab("claude"))
        // A measured pool that carries an api block stays on its provider's tab.
        let measured = try presenter(edit: editAPI("claude") { $0["spend"] = ["month_usd": 1.0] })
        XCTAssertTrue(measured.tabs.contains { $0.id == "claude" })
        XCTAssertFalse(measured.isAPITab("claude"))
    }

    func testSpendAgainstTheBudgetUsesThePaceBarAndSaysSurplusOrDeficit() throws {
        let p = try presenter()
        let page = try XCTUnwrap(p.providerPage(for: "openrouter-free", show: .used))
        XCTAssertTrue(page.isAPI)
        XCTAssertEqual(page.available, ["spend", "balance", "models", "activity", "status", "links"])
        let spend = try XCTUnwrap(page.spend)
        XCTAssertEqual(spend.basis, .budget)
        XCTAssertEqual(spend.figure, "$6.61")
        XCTAssertEqual(spend.detail, "of $20 budget")
        XCTAssertEqual(spend.today, "$0.00")
        XCTAssertEqual(spend.week, "$0.09")
        XCTAssertEqual(spend.month, "$6.61")
        XCTAssertEqual(try XCTUnwrap(spend.used), 33.05, accuracy: 0.01)
        XCTAssertEqual(spend.pace?.kind, .surplus, "a third of the budget by day 27 of 30")
        XCTAssertEqual(spend.bar?.segments.last?.kind, .surplus)
        XCTAssertNotNil(spend.bar?.tick)
        XCTAssertEqual(spend.source, "OpenRouter account")
        // The operator's own budget wins over the reading's.
        var panel = PanelPrefs()
        panel.budgets["openrouter-free"] = 7
        let tight = try presenter(panel).providerPage(for: "openrouter-free", show: .used)!.spend!
        XCTAssertEqual(tight.detail, "of $7 budget")
        XCTAssertEqual(tight.pace?.kind, .deficit)
        XCTAssertEqual(tight.bar?.segments.last?.kind, .deficit)
        XCTAssertNotNil(tight.runsOut, "at this rate the budget is gone before the month ends")
        let left = try presenter(panel).providerPage(for: "openrouter-free", show: .left)!.spend!
        XCTAssertEqual(left.detail, "$0.39 left of $7")
    }

    func testWithoutABudgetTheBarIsTheKeyLimitThenTheCreditThenNothing() throws {
        let noBudget = editAPI("openrouter-free") { $0["budget"] = nil }
        let key = try XCTUnwrap(presenter(edit: noBudget).providerPage(for: "openrouter-free", show: .used)?.spend)
        XCTAssertEqual(key.basis, .keyLimit)
        XCTAssertEqual(try XCTUnwrap(key.used), 66.1, accuracy: 0.01)
        XCTAssertEqual(key.detail, "of $10 key limit")
        XCTAssertNil(key.pace, "no pace tick without a budget")
        let credit = try XCTUnwrap(presenter(edit: { object in
            noBudget(&object)
            editAPI("openrouter-free") { $0["key_limit_usd"] = nil; $0["key_remaining_usd"] = nil }(&object)
        }).providerPage(for: "openrouter-free", show: .used)?.spend)
        XCTAssertEqual(credit.basis, .credit)
        XCTAssertEqual(credit.detail, "of $26 credit")
        XCTAssertTrue(credit.hot, "$1.79 of $26 left")
        let bare = try XCTUnwrap(presenter(edit: { object in
            noBudget(&object)
            editAPI("openrouter-free") { for k in ["key_limit_usd", "key_remaining_usd", "balance_usd", "credits_usd"] { $0[k] = nil } }(&object)
        }).providerPage(for: "openrouter-free", show: .used)?.spend)
        XCTAssertEqual(bare.basis, .none)
        XCTAssertNil(bare.bar)
        XCTAssertEqual(bare.figure, "$6.61")
    }

    func testGeminiApiIsMeasuredByCrossfeedAndTheOperatorsBudgetMakesItADeficit() throws {
        var panel = PanelPrefs()
        panel.budgets["gemini-metered"] = 14
        let page = try XCTUnwrap(presenter(panel).providerPage(for: "gemini-metered", show: .used))
        let spend = try XCTUnwrap(page.spend)
        XCTAssertEqual(spend.figure, "$12.84")
        XCTAssertEqual(spend.source, "Measured by Crossfeed")
        XCTAssertEqual(spend.pace?.kind, .deficit)
        XCTAssertEqual(spend.runsOut, "Runs out in 2d 9h")
        XCTAssertNil(page.balance, "no credit or key limit is reported")
        XCTAssertEqual(page.models.map(\.name), ["gemini-3.1-pro-preview", "gemini-flash-latest", "gemini-flash-lite-latest"])
        XCTAssertEqual(page.models.first?.usd, "$9.90")
        XCTAssertEqual(page.models.first?.share, 1)
        XCTAssertNil(page.models.first?.vendor)
    }

    func testCrossfeedChatCountsRunsWhenItsRelayReportsNoMoney() throws {
        let page = try XCTUnwrap(presenter().providerPage(for: "chatgpt-work", show: .used))
        let spend = try XCTUnwrap(page.spend)
        XCTAssertTrue(spend.figure.hasSuffix("runs"), spend.figure)
        XCTAssertNil(spend.bar)
        XCTAssertEqual(spend.detail, "no cost reported")
        XCTAssertEqual(page.models.first?.name, "GPT-6 · High")
        XCTAssertEqual(page.models.first?.usd, "61 runs", "ranked by runs when no money is reported")
        XCTAssertEqual(page.activity?.metric, .requests, "the chart plots what the relay has")
        XCTAssertEqual(page.activity?.available, [.tokens, .requests])
    }

    func testBalanceHasCreditAndKeyLimitRowsAndLinksNameBillingWhenKnown() throws {
        let page = try XCTUnwrap(presenter().providerPage(for: "openrouter-free", show: .used))
        XCTAssertEqual(page.balance?.rows.map(\.label), ["Credit", "Key limit"])
        XCTAssertEqual(page.balance?.rows.map(\.figure), ["$1.79", "$3.39"])
        XCTAssertEqual(page.balance?.rows.map(\.detail), ["left of $26", "left of $10"])
        XCTAssertEqual(page.links.map(\.title), ["Usage and cost", "Status", "Billing"])
        XCTAssertEqual(page.links[2].kind, .billing(URL(string: "https://openrouter.ai/settings/credits")!))
        XCTAssertEqual(page.balanceURL, URL(string: "https://openrouter.ai/settings/credits"))
        XCTAssertEqual(page.links[0].kind, .usage("openrouter-free"))
        let gemini = try XCTUnwrap(presenter().providerPage(for: "chatgpt-work", show: .used))
        XCTAssertFalse(gemini.links.contains { $0.title == "Billing" }, "no billing page is known for the relay")
        XCTAssertFalse(page.links.contains { $0.kind == .console }, "the footer already opens the console")
    }

    func testTheActivityChartFollowsTheMetricAndRangeTheOperatorPicked() throws {
        let tab = "openrouter-free"
        let first = try XCTUnwrap(presenter().providerPage(for: tab, show: .used)?.activity)
        XCTAssertEqual(first.metric, .cost, "an API tab plots what it is billed in")
        XCTAssertEqual(first.available, [.tokens, .cost, .requests])
        XCTAssertEqual(first.days.count, 14)
        XCTAssertEqual(first.caption, " spent today")
        XCTAssertEqual(first.today, "$0.00")
        var panel = PanelPrefs()
        panel.setOption(tab, "activity") { $0.metric = .requests; $0.days = 30 }
        let requests = try XCTUnwrap(presenter(panel).providerPage(for: tab, show: .used)?.activity)
        XCTAssertEqual(requests.metric, .requests)
        XCTAssertEqual(requests.days.count, 30)
        XCTAssertEqual(requests.caption, " requests today")
        panel.setOption(tab, "activity") { $0.metric = .tokens; $0.days = 7 }
        let tokens = try XCTUnwrap(presenter(panel).providerPage(for: tab, show: .used)?.activity)
        XCTAssertEqual(tokens.days.count, 7)
        XCTAssertEqual(tokens.metric, .tokens)
        panel.setOption(tab, "activity") { $0.metric = .cost; $0.days = 7 }
        let cost = try XCTUnwrap(presenter(panel).providerPage(for: tab, show: .used)?.activity)
        XCTAssertEqual(cost.days.count, 7)
        XCTAssertEqual(cost.days.reduce(0, +), 9, "last 7 days at $0.09, in cents")
        // A metric the pool does not have falls back to the default.
        var claude = PanelPrefs()
        claude.setOption("claude", "tokens") { $0.metric = .cost; $0.days = 7 }
        let tab14 = try XCTUnwrap(presenter(claude).providerPage(for: "claude", show: .used)?.tokens)
        XCTAssertEqual(tab14.metric, .tokens)
        XCTAssertEqual(tab14.days.count, 7)
        XCTAssertEqual(tab14.available, [.tokens])
    }

    func testStatusIsATileNotAPinnedLine() throws {
        let p = try presenter()
        let claude = try XCTUnwrap(p.providerPage(for: "claude", show: .used))
        XCTAssertEqual(claude.status?.word, "Minor issue")
        XCTAssertEqual(claude.status?.level, .notice)
        XCTAssertEqual(claude.status?.url, URL(string: "https://status.claude.com/"))
        XCTAssertTrue(claude.available.contains("status"))
        XCTAssertEqual(p.providerPage(for: "copilot", show: .used)?.status?.level, .clear)
        XCTAssertEqual(p.providerPage(for: "openrouter-free", show: .used)?.status?.word, "No incidents")
        XCTAssertEqual(p.providerPage(for: "gemini-metered", show: .used)?.status?.level, .unknown, "no feed: not reported")
    }

    func testOverviewListsTheIncidentsAcrossProvidersAndHasAnApiCardEach() throws {
        let p = try presenter()
        let page = p.overviewPage(order: p.tabs.map(\.id), show: .used)
        XCTAssertEqual(page.incidents.map(\.id), ["claude", "go"])
        XCTAssertEqual(page.incidents.first?.word, "Minor issue")
        XCTAssertNotNil(page.incidents.first?.url)
        let quiet = try presenter(edit: { object in
            var pools = object["pools"] as! [[String: Any]]
            for i in pools.indices { pools[i]["incident"] = nil }
            object["pools"] = pools
        })
        XCTAssertEqual(quiet.overviewPage(order: quiet.tabs.map(\.id), show: .used).incidents, [])
        let cards = Dictionary(uniqueKeysWithValues: page.providers.map { ($0.id, $0) })
        XCTAssertEqual(cards["openrouter-free"]?.line?.figure, "$6.61 spent")
        XCTAssertEqual(cards["openrouter-free"]?.line?.window, "This month")
        XCTAssertEqual(cards["openrouter-free"]?.line?.resets, "$20 budget")
        XCTAssertEqual(cards["gemini-metered"]?.line?.figure, "$12.84")
        XCTAssertEqual(cards["gemini-metered"]?.line?.resets, "no budget")
        XCTAssertTrue(cards["chatgpt-work"]?.line?.figure.hasSuffix("runs") == true)
        XCTAssertEqual(page.links.map(\.title), ["Usage and cost"])
    }

    func testAnApiTabsStripBarShowsSpendAgainstItsBudget() throws {
        var panel = PanelPrefs()
        panel.budgets["gemini-metered"] = 14
        let tabs = Dictionary(uniqueKeysWithValues: try presenter(panel).tabs.map { ($0.id, $0) })
        XCTAssertEqual(try XCTUnwrap(tabs["gemini-metered"]?.used), 91.7, accuracy: 0.1)
        XCTAssertEqual(tabs["gemini-metered"]?.pace, .deficit)
        XCTAssertEqual(try XCTUnwrap(tabs["openrouter-free"]?.used), 33.05, accuracy: 0.01)
        XCTAssertNil(tabs["chatgpt-work"]?.used)
    }

    func testAPoolWithNoApiBlockStillGetsATabAndSaysNoSpendWasRead() throws {
        let p = try presenter(edit: { object in
            var pools = object["pools"] as! [[String: Any]]
            for i in pools.indices where pools[i]["pool"] as? String == "gemini-metered" { pools[i]["api"] = nil }
            object["pools"] = pools
        })
        let page = try XCTUnwrap(p.providerPage(for: "gemini-metered", show: .used))
        XCTAssertTrue(page.isAPI)
        XCTAssertEqual(page.spend?.figure, "Not read")
        XCTAssertEqual(page.updated, "No reading")
        XCTAssertEqual(page.available, ["spend", "status", "links"])
    }
}
