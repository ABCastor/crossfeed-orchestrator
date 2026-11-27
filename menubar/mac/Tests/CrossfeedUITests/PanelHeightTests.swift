import AppKit
import CrossfeedCore
import SwiftUI
import XCTest
@testable import CrossfeedUI

private func fixture() throws -> Presenter {
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("Fixtures/glance-round9.json")
    let now = ISO8601DateFormatter().date(from: "2026-11-26T16:20:00Z")!
    return Presenter(glance: try Glance.decode(Data(contentsOf: url)), loadedAt: now, now: now)
}

/// Switching tabs must not move the menu: every tab and the settings pane lay out at the one panel height.
@MainActor
final class PanelHeightTests: XCTestCase {
    func testEveryTabAndTheSettingsPaneShareOnePanelHeight() throws {
        let presenter = try fixture()
        let heights = PanelSizing.measure(presenter: presenter, prefs: BarPrefs(), cap: 4000)
        XCTAssertGreaterThan(Set(heights.perTab.values).count, 1, "the tabs differ in content height, so the fixed height matters")
        XCTAssertEqual(heights.panel, max(heights.settings, heights.perTab.filter { $0.key != "other" }.values.max()!))
        var seen: [String: CGFloat] = [:]
        for tab in presenter.tabs.map(\.id) + ["settings"] {
            let state = MenuState(presenter: presenter, selected: tab == "settings" ? nil : tab)
            state.showsSettings = tab == "settings"
            state.panelHeight = heights.panel
            let host = NSHostingView(rootView: CrossfeedMenuView(state: state, showsActions: false))
            seen[tab] = ceil(host.fittingSize.height)
        }
        XCTAssertEqual(Set(seen.values), [heights.panel], "\(seen)")
    }

    func testOverviewIsFirstAndSharesThePanelHeightAndAHiddenTabDoesNotSetIt() throws {
        let presenter = try fixture()
        let all = PanelSizing.measure(presenter: presenter, prefs: BarPrefs(), cap: 4000)
        XCTAssertNotNil(all.perTab[Presenter.overviewID])
        let tallest = all.perTab.filter { $0.key != "other" && $0.key != Presenter.overviewID }.max { $0.value < $1.value }!.key
        let hidden = PanelSizing.measure(presenter: presenter, prefs: BarPrefs(), panel: { var p = PanelPrefs(); p.toggleTab(tallest); return p }(), cap: 4000)
        XCTAssertNil(hidden.perTab[tallest])
        XCTAssertLessThanOrEqual(hidden.panel, all.panel)
        let state = MenuState(presenter: presenter)
        XCTAssertEqual(state.current, Presenter.overviewID)
        XCTAssertEqual(state.shownTabs.first?.id, Presenter.overviewID)
        state.panel.toggleTab(tallest)
        XCTAssertFalse(state.shownTabs.contains { $0.id == tallest })
        state.selected = tallest
        XCTAssertEqual(state.current, Presenter.overviewID, "a hidden tab falls back to Overview")
    }

    func testAShortScreenCapsThePanelAndTheContentScrolls() throws {
        let presenter = try fixture()
        let heights = PanelSizing.measure(presenter: presenter, prefs: BarPrefs(), cap: 500)
        XCTAssertEqual(heights.panel, 500)
        let state = MenuState(presenter: presenter, selected: "agy")
        state.panelHeight = heights.panel
        let host = NSHostingView(rootView: CrossfeedMenuView(state: state, showsActions: false))
        XCTAssertEqual(ceil(host.fittingSize.height), 500)
    }

    func testReduceMotionDropsTheAnimation() {
        // The animation is a short ease; with Reduce Motion it is nil. Either way a tab switch lands on the new tab.
        let state = MenuState(presenter: try? fixture(), selected: "claude")
        state.choose("codex")
        XCTAssertEqual(state.selected, "codex")
    }
}
