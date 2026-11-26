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
    public static func measure(presenter: Presenter, prefs: BarPrefs, failure: String? = nil, cap: CGFloat) -> PanelHeights {
        func height(tab: String?, settings: Bool) -> CGFloat {
            let state = MenuState(presenter: presenter, selected: tab)
            state.prefs = prefs
            state.showsSettings = settings
            state.actionFailure = failure
            let host = NSHostingView(rootView: CrossfeedMenuView(state: state, showsActions: false))
            return ceil(host.fittingSize.height)
        }
        var perTab: [String: CGFloat] = [:]
        for tab in presenter.tabs { perTab[tab.id] = height(tab: tab.id, settings: false) }
        return PanelHeights(perTab: perTab, settings: height(tab: nil, settings: true), cap: cap)
    }
}
