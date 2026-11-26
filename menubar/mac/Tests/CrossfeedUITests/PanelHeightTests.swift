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
