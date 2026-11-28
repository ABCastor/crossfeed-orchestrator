import AppKit
import CrossfeedCore
import SwiftUI
import XCTest
@testable import CrossfeedUI

private func presenter(_ name: String = "glance-round12.json") throws -> Presenter {
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("Fixtures/" + name)
    let now = ISO8601DateFormatter().date(from: "2026-11-27T10:02:00Z")!
    return Presenter(glance: try Glance.decode(Data(contentsOf: url)), loadedAt: now, now: now, timeZone: TimeZone(identifier: "Europe/Rome")!)
}

private func height(_ state: MenuState) -> CGFloat {
    ceil(NSHostingView(rootView: CrossfeedMenuView(state: state, showsActions: false)).fittingSize.height)
}

@MainActor
final class Round12UITests: XCTestCase {
    func testChoosingATabWritesItAsTheLastTabAndANoOpWritesNothing() throws {
        let state = MenuState(presenter: try presenter(), selected: Presenter.overviewID)
        var saved: [PanelPrefs] = []
        state.onPanelChange = { saved.append($0) }
        state.choose("claude")
        XCTAssertEqual(state.panel.lastTab, "claude")
        XCTAssertEqual(saved.last?.lastTab, "claude")
        state.choose("claude")
        XCTAssertEqual(saved.count, 1, "the tab is already open: nothing is written")
        state.choose(Presenter.overviewID)
        XCTAssertEqual(saved.last?.lastTab, Presenter.overviewID, "Overview is a last tab too")
        XCTAssertEqual(saved.count, 2)
    }

    func testWhereTheMenuOpensFollowsOpenOn() throws {
        let state = MenuState(presenter: try presenter(), selected: Presenter.overviewID)
        let shown = state.shownTabs.map(\.id)
        state.panel.lastTab = "openrouter-free"
        XCTAssertEqual(state.panel.startTab(shown: shown), "openrouter-free")
        state.panel.openOn = "codex"
        XCTAssertEqual(state.panel.startTab(shown: shown), "codex")
        state.panel.toggleTab("codex")
        XCTAssertEqual(state.panel.startTab(shown: state.shownTabs.map(\.id)), Presenter.overviewID, "a hidden tab is not opened on")
    }

    func testWhereAButtonSitsInATileTheSmallerFrameTakesTheClick() throws {
        let state = MenuState(presenter: try presenter(), selected: "claude")
        let tile = MenuControl.at("tile:claude/links", .usage("claude")), button = MenuControl.at("link:Status", .url(URL(string: "https://example.org/")!))
        state.controlFrames = [tile: CGRect(x: 0, y: 0, width: 300, height: 60), button: CGRect(x: 200, y: 20, width: 80, height: 20)]
        var routed: MenuControl?
        state.onControl = { routed = $0 }
        XCTAssertTrue(state.routeClick(at: CGPoint(x: 220, y: 30)))
        XCTAssertEqual(routed, button)
        XCTAssertTrue(state.routeClick(at: CGPoint(x: 20, y: 30)))
        XCTAssertEqual(routed, tile, "the rest of the tile opens its page")
        XCTAssertEqual(LinkTarget.usage("claude").control, .usage("claude"))
        XCTAssertEqual(LinkTarget.tab("go").control, .openTab("go"))
    }

    func testACrowdedStripKeepsOnlyTheSelectedName() throws {
        let p = try presenter()
        let state = MenuState(presenter: p, selected: "openrouter-free")
        XCTAssertEqual(state.shownTabs.count, 9, "Overview and eight providers")
        XCTAssertTrue(ProviderSwitcher.needsCompact(state.shownTabs))
        let few = [TabModel(id: "overview", title: "Overview", logo: nil, used: nil, pace: .unknown, hot: false),
                   TabModel(id: "claude", title: "Claude", logo: .claude, used: nil, pace: .unknown, hot: false),
                   TabModel(id: "codex", title: "Codex", logo: .openai, used: nil, pace: .unknown, hot: false)]
        XCTAssertFalse(ProviderSwitcher.needsCompact(few))
        // The old seven tabs still fit with their names, as before.
        let seven = ["Overview", "Claude", "Codex", "Copilot", "AGY", "OpenCode", "Other"].map { TabModel(id: $0, title: $0, logo: nil, used: nil, pace: .unknown, hot: false) }
        XCTAssertFalse(ProviderSwitcher.needsCompact(seven))
    }

    func testEveryTabAndEverySettingsPageShareOnePanelHeightWithTheApiTabs() throws {
        let p = try presenter()
        let heights = PanelSizing.measure(presenter: p, prefs: BarPrefs(), cap: 4000)
        for tab in ["openrouter-free", "gemini-metered", "chatgpt-work"] { XCTAssertNotNil(heights.perTab[tab], tab) }
        for tab in p.tabs.map(\.id) + ["settings0", "settings1", "settings2", "settings3"] {
            let state = MenuState(presenter: p, selected: tab.hasPrefix("settings") ? nil : tab)
            state.showsSettings = tab.hasPrefix("settings")
            state.settingsPage = Int(tab.dropFirst(8)) ?? 0
            state.panelHeight = heights.panel
            XCTAssertEqual(height(state), heights.panel, tab)
        }
    }

    func testTheStatusTileOnOverviewComesWithAnIncidentAndGoesWithoutOne() throws {
        let busy = try presenter()
        let state = MenuState(presenter: busy, selected: Presenter.overviewID)
        let with = height(state)
        state.panel.setTile(Presenter.overviewID, "status", on: false)
        XCTAssertLessThan(height(state), with - 40, "hiding it takes its rows away")
        XCTAssertGreaterThan(with, 0)
    }

    func testAChartTilesOptionsAreWhatItDrawsAndWhatSettingsOffers() throws {
        let state = MenuState(presenter: try presenter(), selected: "openrouter-free")
        let choice = try XCTUnwrap(state.chartOptions(tab: "openrouter-free", tile: "activity"))
        XCTAssertEqual(choice.metrics, [.tokens, .cost, .requests])
        XCTAssertEqual(choice.metric, .cost)
        XCTAssertEqual(choice.days, 14)
        state.updatePanel { $0.setOption("openrouter-free", "activity") { $0.days = 30; $0.metric = .requests } }
        let changed = try XCTUnwrap(state.chartOptions(tab: "openrouter-free", tile: "activity"))
        XCTAssertEqual(changed.metric, .requests)
        XCTAssertEqual(changed.days, 30)
        XCTAssertNotNil(state.chartOptions(tab: "claude", tile: "tokens"))
        XCTAssertNil(state.chartOptions(tab: "claude", tile: "limits"))
        XCTAssertEqual(state.chartOptions(tab: Presenter.overviewID, tile: "tokens")?.metrics, [.tokens, .cost, .requests], "Overview adds up every pool, API spend included")
    }

    func testABudgetChangesWhatTheSpendTileSaysWithoutANewReading() throws {
        let state = MenuState(presenter: try presenter(), selected: "gemini-metered")
        XCTAssertNil(state.live?.providerPage(for: "gemini-metered", show: .used)?.spend?.pace)
        state.updatePanel { $0.budgets["gemini-metered"] = 14 }
        XCTAssertEqual(state.live?.providerPage(for: "gemini-metered", show: .used)?.spend?.pace?.kind, .deficit)
        XCTAssertEqual(state.orderedTabs.first { $0.id == "gemini-metered" }?.pace, .deficit, "the strip's bar follows too")
    }
}
