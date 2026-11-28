import AppKit
import CrossfeedCore
import SwiftUI
import XCTest
@testable import CrossfeedUI

private func presenter() throws -> Presenter {
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("Fixtures/glance-round11-plugins.json")
    let now = ISO8601DateFormatter().date(from: "2026-11-27T10:02:00Z")!
    return Presenter(glance: try Glance.decode(Data(contentsOf: url)), loadedAt: now, now: now, timeZone: TimeZone(identifier: "Europe/Rome")!)
}

@MainActor
final class TileStateTests: XCTestCase {
    func testEveryTabAndEverySettingsPageSharesOnePanelHeightOnTheTileDesign() throws {
        let p = try presenter()
        let heights = PanelSizing.measure(presenter: p, prefs: BarPrefs(), cap: 4000)
        XCTAssertNotNil(heights.perTab[Presenter.overviewID])
        for tab in p.tabs.map(\.id) + ["settings0", "settings1", "settings2", "settings3"] {
            let state = MenuState(presenter: p, selected: tab.hasPrefix("settings") ? nil : tab)
            state.showsSettings = tab.hasPrefix("settings")
            state.settingsPage = Int(tab.dropFirst(8)) ?? 0
            state.panelHeight = heights.panel
            let host = NSHostingView(rootView: CrossfeedMenuView(state: state, showsActions: false))
            XCTAssertEqual(ceil(host.fittingSize.height), heights.panel, tab)
        }
    }

    func testAPluginTileDrawsOnOverviewOnlyOnceTheOperatorAddedIt() throws {
        let p = try presenter()
        func height(_ prefs: PanelPrefs) -> CGFloat {
            let state = MenuState(presenter: p, selected: Presenter.overviewID)
            state.panel = prefs
            return ceil(NSHostingView(rootView: CrossfeedMenuView(state: state, showsActions: false)).fittingSize.height)
        }
        var prefs = PanelPrefs()
        let without = height(prefs)
        prefs.setTile("overview", "plugin:gh-actions/runs", on: true)
        XCTAssertGreaterThan(height(prefs), without + 80)
    }

    func testADragCommitsTheNewTileOrderToPanelPrefsAndOverviewsProvidersBecomeTheTabOrder() throws {
        let p = try presenter()
        let state = MenuState(presenter: p, selected: "claude")
        var saved: [PanelPrefs] = []
        state.onPanelChange = { saved.append($0) }
        state.onReorderBlocks("claude", ["links", "limits", "resets", "tokens", "policy"])
        XCTAssertEqual(Array(state.panel.tiles["claude"]!.prefix(5)), ["links", "limits", "resets", "tokens", "policy"])
        XCTAssertEqual(saved.count, 1)
        state.onReorderBlocks(Presenter.overviewID, ["provider:codex", "provider:claude", "provider:copilot", "provider:agy", "provider:go", "links", "subscriptions", "tokens"])
        XCTAssertEqual(state.orderedTabs.map(\.id).prefix(2), ["codex", "claude"])
        XCTAssertEqual(Array(state.panel.tiles["overview"]!.prefix(4)), ["providers", "links", "subscriptions", "tokens"])
        XCTAssertEqual(saved.count, 2)
        state.onReorderBlocks("claude", ["links", "limits", "resets", "tokens", "policy"])
        XCTAssertEqual(saved.count, 2, "nothing changed: nothing is written")
    }

    func testAHiddenTabFallsBackToOverviewAndTheResetFlowClearsWhenTheTabChanges() throws {
        let p = try presenter()
        let state = MenuState(presenter: p, selected: "codex")
        state.resetFlow = ResetFlow(key: "weekly", phase: .confirm)
        state.choose("claude")
        XCTAssertNil(state.resetFlow)
        state.panel.toggleTab("claude")
        XCTAssertEqual(state.current, Presenter.overviewID)
    }
}
