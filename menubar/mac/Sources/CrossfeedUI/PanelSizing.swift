// Crossfeed menu: one panel height for every provider tab and the settings pane, so switching never moves
// the menu. The height is the tallest of them, held to the screen; whatever is taller scrolls inside.
import AppKit
import CrossfeedCore
import SwiftUI

public struct PanelHeights: Equatable {
    public var perTab: [String: CGFloat]
    public var settings: CGFloat
    public var cap: CGFloat
    /// The panel: the tallest content, never above the cap. "Other" (pools with no percentage window, listed with all
    /// their models) is a catch-all that can run to thousands of points; it scrolls instead of setting the height.
    public var panel: CGFloat { min(cap, max(settings, perTab.filter { $0.key != "other" }.values.max() ?? 0)) }
}

public enum PanelSizing {
    @MainActor
    public static func measure(presenter: Presenter, prefs: BarPrefs, panel: PanelPrefs = PanelPrefs(),
                               failure: String? = nil, cap: CGFloat) -> PanelHeights {
        func height(tab: String?, settings: Bool, page: Int = 0) -> CGFloat {
            let state = MenuState(presenter: presenter, selected: tab)
            state.prefs = prefs
            state.panel = panel
            state.showsSettings = settings
            state.settingsPage = page
            state.actionFailure = failure
            let host = NSHostingView(rootView: CrossfeedMenuView(state: state, showsActions: false))
            return ceil(host.fittingSize.height)
        }
        var perTab: [String: CGFloat] = [Presenter.overviewID: height(tab: Presenter.overviewID, settings: false)]
        // A hidden provider is not in the strip, so it does not set the panel height.
        for tab in presenter.tabs where panel.isTabVisible(tab.id) { perTab[tab.id] = height(tab: tab.id, settings: false) }
        return PanelHeights(perTab: perTab, settings: (0...3).map { height(tab: nil, settings: true, page: $0) }.max() ?? 0, cap: cap)
    }
}
