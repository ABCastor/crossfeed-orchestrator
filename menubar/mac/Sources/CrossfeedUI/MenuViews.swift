// Crossfeed menu: the views of the open menu, B+ Tiles.
//
// A strip of tabs over a sunk panel; each tab is a header and rounded tiles. The strip's structure follows
// CodexBar's menu (MIT, Copyright (c) 2026 Peter Steinberger): provider switcher with a mini usage bar under each tab.
// Everything else is Crossfeed's: the tile parts are in Tiles.swift, the pace bar in PaceBar.swift.
import AppKit
import CrossfeedCore
import SwiftUI

/// Every button the host routes by position, because a click inside an open NSMenu reaches the hosting view, not SwiftUI.
public enum MenuControl: Hashable {
    case refresh, settings, back, reset, notifyLow, notifyReset
    case openTab(String), usage(String?), console, openURL(URL), toggleTab(String), moveTab(String, Int), settingsPage(Int)
    case show(String), window(String), count(BarPrefs.Count), icon(BarPrefs.Icon), text(BarPrefs.Text), extra(BarPrefs.Extra), resets(BarPrefs.Reset), refreshEvery(Int)
    /// Settings, Tiles page: Used or Left, which tab's tiles are listed, a tile on or off, a tile one place up or down, Add tile.
    case panelShow(PanelPrefs.Show), settingsTab(String), toggleTile(String, String), moveTile(String, String, Int), addOpen, addTile(String, String)
    /// The spend policy popup, and the reset flow: Use, Cancel, Use (confirmed).
    case policyOpen, resetAsk(String), resetCancel, resetUse(String)
}

/// The one reset being asked about, run, or reported.
public struct ResetFlow: Equatable {
    public enum Phase: Equatable { case confirm, running, done(Int), failed(String) }
    public var key: String
    public var phase: Phase
    public init(key: String, phase: Phase) { self.key = key; self.phase = phase }
}

/// What the menu needs from its host. All state lives here so a snapshot and the live menu share it.
public final class MenuState: ObservableObject {
    @Published public var presenter: Presenter?
    @Published public var selected: String?
    @Published public var failure: String?
    @Published public var actionFailure: String?
    @Published public var edits = PolicyEdits()
    /// What the menu bar item shows, and whether the menu is on its settings pane.
    @Published public var prefs = BarPrefs()
    @Published public var showsSettings = false
    /// Which page of the settings is open: 0 tiles, 1 tabs, 2 the menu bar item.
    @Published public var settingsPage = 0
    /// Settings, Tiles page: the tab whose tiles are listed, and whether the Add tile list is open.
    @Published public var settingsTab = Presenter.overviewID
    @Published public var addOpen = false
    /// panel.json: Used or Left, the tab order and which are shown, the tiles of every tab.
    @Published public var panel = PanelPrefs()
    @Published public var policyOpen = false
    @Published public var resetFlow: ResetFlow?
    @Published public var isReloading = false
    /// One height for every tab and the settings pane, set by the host so the panel never jumps; the content scrolls inside.
    /// Nil sizes the menu to its content (what the height is measured from).
    @Published public var panelHeight: CGFloat?
    public var onControl: (MenuControl) -> Void = { _ in }
    public var onPanelChange: (PanelPrefs) -> Void = { _ in }

    /// A short crossfade and a few points of slide; none when the system asks for less motion.
    public static var animation: Animation? {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion ? nil : .easeOut(duration: 0.18)
    }

    /// Switch provider tab. The page fades and slides in; the panel keeps its height.
    public func choose(_ id: String) {
        guard id != current else { return }
        policyOpen = false
        resetFlow = nil
        withAnimation(Self.animation) { selected = id }
    }

    /// Writes one change to panel.json through the host, and shows it at once.
    public func updatePanel(_ change: (inout PanelPrefs) -> Void) {
        var next = panel
        change(&next)
        guard next != panel else { return }
        panel = next
        onPanelChange(next)
    }

    /// The tab on screen: Overview unless a provider tab that is still shown was chosen.
    public var current: String {
        guard let selected, selected != Presenter.overviewID, panel.isTabVisible(selected),
              orderedTabs.contains(where: { $0.id == selected }) else { return Presenter.overviewID }
        return selected
    }

    /// Overview, then the provider tabs that are shown.
    public var shownTabs: [TabModel] {
        [TabModel(id: Presenter.overviewID, title: "Overview", logo: nil, used: nil, pace: .unknown, hot: false)]
            + orderedTabs.filter { panel.isTabVisible($0.id) }
    }
    public var canEditPolicy = true
    public var onPolicyChange: (PolicyChange) -> Void = { _ in }
    public var onSelect: (String) -> Void = { _ in }
    public var onReorder: (String, String) -> Void = { _, _ in }
    @Published var draggedTile: String?
    /// The reorderable tiles of a tab (CrossfeedUI/TileReorder.swift): frames, measurements, and the drag.
    @Published var blockDrag: BlockDrag?
    public var blockFrames: [String: BlockFrame] = [:]
    var probes: [String: MetricsProbe] = [:]
    var pressedBlock: (id: String, origin: CGPoint)?
    /// A tab's tiles were put in a new order (ids as the view declared them: `provider:<id>` on Overview).
    public var onReorderBlocks: (String, [String]) -> Void = { _, _ in }
    /// True while a tile is carried or settling; the host listens for Escape only then.
    public var onBlockDragActive: (Bool) -> Void = { _ in }
    @Published var dropTile: String?
    private var pressedTile: (id: String, origin: CGPoint)?
    public var onReload: () -> Void = {}
    public var onAbout: () -> Void = {}
    public var onQuit: () -> Void = {}
    public var onOpenConsole: () -> Void = {}
    /// Tab buttons' rectangles in the menu view's own coordinates, so the host can route a click that the
    /// menu's tracking loop delivers to the view instead of to SwiftUI's button.
    public var tileFrames: [String: CGRect] = [:]
    public var policyFrames: [PolicyChange: CGRect] = [:]
    public var controlFrames: [MenuControl: CGRect] = [:]

    func probe(for tab: String) -> MetricsProbe {
        if let probe = probes[tab] { return probe }
        let probe = MetricsProbe()
        probes[tab] = probe
        return probe
    }

    /// The provider tabs in the saved order. Hidden ones are still here; `shownTabs` drops them.
    public var orderedTabs: [TabModel] {
        guard let tabs = presenter?.tabs else { return [] }
        let byID = Dictionary(uniqueKeysWithValues: tabs.map { ($0.id, $0) })
        return panel.orderedTabs(tabs.map(\.id)).compactMap { byID[$0] }
    }

    public func ownsPointer(at point: CGPoint) -> Bool {
        tile(at: point) != nil || policyFrames.values.contains { $0.contains(point) }
            || controlFrames.values.contains { $0.contains(point) }
            || block(at: point) != nil
    }

    public func beginPointer(at point: CGPoint) {
        cancelPointer()
        pressedTile = tile(at: point).map { ($0, point) }
        pressedBlock = pressedTile == nil ? block(at: point).map { ($0, point) } : nil
    }

    public func movePointer(to point: CGPoint) {
        if blockDrag != nil {
            moveBlockDrag(to: point)
            return
        }
        if let press = pressedBlock {
            if hypot(point.x - press.origin.x, point.y - press.origin.y) >= BlockMotion.slop,
               beginBlockDrag(press.id, press: press.origin) { moveBlockDrag(to: point) }
            return
        }
        guard let press = pressedTile else { return }
        if draggedTile == nil {
            guard press.id != Presenter.overviewID else { return }
            guard hypot(point.x - press.origin.x, point.y - press.origin.y) >= BlockMotion.slop else { return }
            draggedTile = press.id
        }
        let target = tile(at: point)
        if dropTile != target { dropTile = target }
    }

    @discardableResult
    public func endPointer(at point: CGPoint) -> Bool {
        movePointer(to: point)
        defer { cancelPointer() }
        if blockDrag != nil {
            releaseBlockDrag()
            return true // a drag cannot choose a stop or open a link
        }
        if let source = draggedTile {
            if let target = tile(at: point), target != source, target != Presenter.overviewID { onReorder(source, target) }
            return true // a drag cannot select a tab or move a dial
        }
        return routeClick(at: point)
    }

    public func cancelPointer() {
        pressedTile = nil
        pressedBlock = nil
        cancelBlockDrag()
        if draggedTile != nil { draggedTile = nil }
        if dropTile != nil { dropTile = nil }
    }

    private func tile(at point: CGPoint) -> String? {
        tileFrames.first { $0.value.contains(point) }?.key
    }

    @discardableResult
    public func routeClick(at point: CGPoint) -> Bool {
        if let hit = policyFrames.first(where: { $0.value.contains(point) }) {
            if canEditPolicy, edits.pending == nil { onPolicyChange(hit.key) }
            return true
        }
        if let hit = controlFrames.first(where: { $0.value.contains(point) }) {
            onControl(hit.key)
            return true
        }
        if let hit = tile(at: point) {
            onSelect(hit)
            return true
        }
        return false
    }

    public init(presenter: Presenter? = nil, selected: String? = nil, failure: String? = nil) {
        self.presenter = presenter
        self.selected = selected ?? Presenter.overviewID
        self.failure = failure
        onReorderBlocks = { [weak self] tab, ids in self?.commitOrder(tab, ids) }
    }

    /// A drag put a tab's tiles in a new order: write it to panel.json. On Overview the providers' own order is the tab order.
    func commitOrder(_ tab: String, _ ids: [String]) {
        if tab == Presenter.overviewID {
            let providers = presenter?.tabs.map(\.id) ?? []
            updatePanel { $0.setOverviewOrder(ids, providers: providers) }
        } else if tab.hasPrefix("settings:") {
            let target = String(tab.dropFirst("settings:".count))
            let alert = panel.tileOrder(target, available: [TileID.alert]).contains(TileID.alert)
            updatePanel { $0.setTileOrder(target, shown: (alert ? [TileID.alert] : []) + ids) }
        } else {
            updatePanel { $0.setTileOrder(tab, shown: ids) }
        }
    }
}

public enum MenuMetrics {
    /// CodexBar's menuCardBaseWidth.
    public static let width: CGFloat = 310
    static let horizontal: CGFloat = 16
    static let space = "crossfeed-menu"
}

public struct CrossfeedMenuView: View {
    @ObservedObject var state: MenuState
    /// The live menu draws Open console, About and Quit as native menu items, as CodexBar does,
    /// so they keep the system's hover, keyboard and dismissal. The snapshot draws them here.
    let showsActions: Bool

    public init(state: MenuState, showsActions: Bool = true) {
        self.state = state
        self.showsActions = showsActions
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 0) { panes }
                .frame(width: MenuMetrics.width, height: state.panelHeight, alignment: .topLeading)
                .clipped()
            if showsActions {
                MenuDivider(inset: true)
                VStack(alignment: .leading, spacing: 0) {
                    ActionRow(symbol: "arrow.up.right.square", title: "Open console", action: state.onOpenConsole)
                }
                MenuDivider(inset: true)
                VStack(alignment: .leading, spacing: 0) {
                    ActionRow(symbol: nil, title: "About Crossfeed", action: state.onAbout)
                    ActionRow(symbol: nil, title: "Quit Crossfeed", action: state.onQuit)
                }
                .padding(.bottom, 4)
            }
        }
        .frame(width: MenuMetrics.width, alignment: .topLeading)
        .coordinateSpace(name: MenuMetrics.space)
        .onPreferenceChange(TileFrames.self) { state.tileFrames = $0 }
        .onPreferenceChange(PolicyFrames.self) { state.policyFrames = $0 }
        .onPreferenceChange(ControlFrames.self) { state.controlFrames = $0 }
        .onPreferenceChange(BlockFrames.self) { state.blockFrames = $0 }
        .background(CF.sunk)
        .foregroundStyle(CF.ink)
    }

    @ViewBuilder private var panes: some View {
        if state.showsSettings {
            scrolling { SettingsPane(state: state) }
        } else if let presenter = state.presenter {
            ProviderSwitcher(state: state)
                .padding(.horizontal, 6).padding(.top, 8).padding(.bottom, 0)
            scrolling {
                VStack(alignment: .leading, spacing: 0) {
                    let tab = state.current
                    ZStack(alignment: .topLeading) {
                        if tab == Presenter.overviewID {
                            OverviewPageView(presenter: presenter, state: state)
                                .id(tab)
                                .transition(.asymmetric(insertion: .opacity.combined(with: .offset(y: 5)), removal: .opacity))
                        } else if let page = presenter.providerPage(for: tab, show: state.panel.show) {
                            ProviderPageView(page: page, presenter: presenter, state: state)
                                .id(tab)
                                .transition(.asymmetric(insertion: .opacity.combined(with: .offset(y: 5)), removal: .opacity))
                        }
                    }
                    failureBanner
                }
            }
        } else {
            scrolling { VStack(alignment: .leading, spacing: 0) { EmptyCard(text: state.failure ?? "Reading quotas…"); failureBanner } }
        }
    }

    /// With a fixed panel height the content scrolls inside it; without one it is simply laid out (and measured).
    @ViewBuilder private func scrolling<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        if state.panelHeight != nil {
            ScrollView(.vertical, showsIndicators: false) { content().frame(width: MenuMetrics.width, alignment: .leading) }
                .frame(maxHeight: .infinity, alignment: .top)
        } else {
            content()
        }
    }

    @ViewBuilder private var failureBanner: some View {
        if let failure = state.actionFailure {
            Text(failure).font(CF.face(11)).foregroundStyle(CF.humanText)
                .padding(.horizontal, MenuMetrics.horizontal).padding(.vertical, 6)
        }
    }
}

// MARK: Pages

/// The tiles of one tab, in the operator's order, as the views the layout places.
private func pluginIDs(_ presenter: Presenter) -> [String] { presenter.glance.plugins.map(\.id) }

struct OverviewPageView: View {
    let presenter: Presenter
    @ObservedObject var state: MenuState

    private func available(_ page: OverviewPage) -> [String] {
        var ids = [TileID.alert, TileID.providers, TileID.links] + pluginIDs(presenter)
        if page.subscriptions != nil { ids.append(TileID.subscriptions) }
        if page.tokens != nil { ids.append(TileID.tokens) }
        return ids
    }

    private func items(_ page: OverviewPage, ids: [String]) -> [TileItem] {
        let flat = ids.filter { $0 != TileID.alert }.flatMap { $0 == TileID.providers ? page.providers.map { TileID.provider($0.id) } : [$0] }
        return flat.compactMap { id -> TileItem? in
            if id.hasPrefix("provider:"), let card = page.providers.first(where: { TileID.provider($0.id) == id }) {
                return TileItem(id: id, content: AnyView(ProviderCardTile(card: card, state: state)))
            }
            switch id {
            case TileID.subscriptions: return page.subscriptions.map { TileItem(id: id, content: AnyView(SubscriptionsTile(model: $0))) }
            case TileID.tokens: return page.tokens.map { TileItem(id: id, half: false, content: AnyView(TokensTile(tokens: $0))) }
            case TileID.links: return TileItem(id: id, content: AnyView(LinksTile(links: page.links, state: state)))
            default:
                return presenter.glance.plugins.first { $0.id == id }.map {
                    TileItem(id: id, content: AnyView(PluginTile(entry: $0, show: state.panel.show, state: state)))
                }
            }
        }
    }

    var body: some View {
        let shown = state.shownTabs.map(\.id).filter { $0 != Presenter.overviewID && $0 != "other" }
        let page = presenter.overviewPage(order: shown, show: state.panel.show)
        let ids = state.panel.tileOrder(Presenter.overviewID, available: available(page))
        let tiles = items(page, ids: ids)
        VStack(alignment: .leading, spacing: 0) {
            OverviewHeader(alert: ids.contains(TileID.alert) ? page.alert : nil, state: state)
            if tiles.isEmpty {
                EmptyCard(text: "No tiles. Add one in settings.")
            } else {
                ReorderableTiles(tab: Presenter.overviewID, tiles: tiles, state: state)
                    .padding(.horizontal, 8).padding(.bottom, 10)
            }
        }
    }
}

struct ProviderPageView: View {
    let page: ProviderPage
    let presenter: Presenter
    @ObservedObject var state: MenuState

    private func items(ids: [String]) -> [TileItem] {
        ids.compactMap { id -> TileItem? in
            switch id {
            case TileID.limits: return TileItem(id: id, content: AnyView(LimitsTile(groups: page.groups, notMetered: page.notMetered)))
            case TileID.resets: return TileItem(id: id, content: AnyView(ResetsTile(rows: page.resets, state: state)))
            case TileID.tokens: return page.tokens.map { TileItem(id: id, half: true, content: AnyView(TokensTile(tokens: $0))) }
            case TileID.policy:
                return page.policy.map {
                    TileItem(id: id, half: !state.policyOpen, content: AnyView(PolicyTile(policy: $0, state: state, now: presenter.now)))
                }
            case TileID.credits: return page.credits.map { TileItem(id: id, half: true, content: AnyView(CreditsTile(credits: $0))) }
            case TileID.links: return TileItem(id: id, content: AnyView(LinksTile(links: page.links, state: state)))
            default:
                return presenter.glance.plugins.first { $0.id == id }.map {
                    TileItem(id: id, content: AnyView(PluginTile(entry: $0, show: state.panel.show, state: state)))
                }
            }
        }
    }

    var body: some View {
        let tab = page.id
        let tiles = items(ids: state.panel.tileOrder(tab, available: page.available + pluginIDs(presenter)))
        VStack(alignment: .leading, spacing: 0) {
            ProviderHeader(page: page, state: state)
            ReorderableTiles(tab: tab, tiles: tiles, state: state)
                .padding(.horizontal, 8).padding(.bottom, 10)
        }
    }
}

struct OverviewHeader: View {
    let alert: OverviewPage.Alert?
    @ObservedObject var state: MenuState

    var body: some View {
        HStack(alignment: .center, spacing: 6) {
            if let alert {
                HStack(spacing: 6) {
                    if alert.hot { Triangle().fill(CF.human).frame(width: 8, height: 7) }
                    else { Circle().fill(CF.between).frame(width: 6, height: 6) }
                    Text(alert.text).font(CF.face(13, .semibold)).monospacedDigit()
                        .foregroundStyle(alert.hot ? CF.humanText : CF.ink).lineLimit(1)
                }
                .accessibilityElement(children: .combine)
            }
            Spacer(minLength: 4)
            HeaderButton(symbol: "arrow.clockwise", label: state.isReloading ? "Updating" : "Refresh now", control: .refresh, state: state)
            HeaderButton(symbol: "slider.horizontal.3", label: "Settings", control: .settings, state: state)
        }
        .padding(.leading, 16).padding(.trailing, 10).padding(.top, 8).padding(.bottom, 8)
    }
}

struct ProviderHeader: View {
    let page: ProviderPage
    @ObservedObject var state: MenuState

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 8) {
                if let logo = page.logo { ProviderMark(logo: logo, size: 18) }
                Text(page.title).font(CF.face(16, .semibold)).lineLimit(1).layoutPriority(1)
                if let plan = page.plan { Text(plan).font(CF.face(13)).foregroundStyle(CF.quiet).lineLimit(1).padding(.leading, -2) }
                Spacer(minLength: 4)
                if let badge = page.badge {
                    HStack(spacing: 5) {
                        Circle().fill(badge.hot ? CF.human : CF.quiet).frame(width: 6, height: 6)
                        Text(badge.word).font(CF.face(12, .medium)).foregroundStyle(badge.hot ? CF.humanText : CF.quiet)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
            .frame(minHeight: 20)
            HStack(alignment: .center, spacing: 0) {
                Text(state.isReloading ? "Updating…" : page.updated).font(CF.face(12)).foregroundStyle(CF.quiet).lineLimit(1)
                Spacer(minLength: 4)
                HeaderButton(symbol: "arrow.clockwise", label: "Refresh now", control: .refresh, state: state)
                HeaderButton(symbol: "slider.horizontal.3", label: "Settings", control: .settings, state: state)
                    .padding(.trailing, -6)
            }
            .padding(.top, 2)
            if let incident = page.incident {
                HStack(alignment: .top, spacing: 7) {
                    Circle().fill(CF.human).frame(width: 6, height: 6).padding(.top, 5)
                    (Text(incident.word).font(CF.face(12, .semibold)).foregroundColor(incident.severe ? CF.humanText : CF.ink)
                        + Text(" · " + incident.text).font(CF.face(12)).foregroundColor(CF.ink))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.top, 8)
                .accessibilityElement(children: .combine)
            }
        }
        .padding(.horizontal, 16).padding(.top, 6).padding(.bottom, 10)
    }
}

// MARK: Provider switcher

struct ProviderSwitcher: View {
    @ObservedObject var state: MenuState

    var body: some View {
        let tabs = state.shownTabs
        let source = tabs.firstIndex { $0.id == state.draggedTile }
        HStack(spacing: 0) {
            ForEach(Array(tabs.enumerated()), id: \.element.id) { index, tab in
                if index > 0 { Spacer(minLength: 0) }
                ProviderTile(tab: tab, isSelected: tab.id == state.current, show: state.panel.show) { state.onSelect(tab.id) }
                    .opacity(tab.id == state.draggedTile ? 0.5 : 1)
                    .overlay(alignment: (source ?? index) < index ? .trailing : .leading) {
                        if tab.id == state.dropTile && tab.id != state.draggedTile {
                            Capsule().fill(CF.machine).frame(width: 2).padding(.vertical, 4)
                                .allowsHitTesting(false).accessibilityHidden(true)
                        }
                    }
                    .accessibilityAction(named: "Move left") {
                        if index > 1, tab.id != Presenter.overviewID { state.onReorder(tab.id, tabs[index - 1].id) }
                    }
                    .accessibilityAction(named: "Move right") {
                        if index + 1 < tabs.count, tab.id != Presenter.overviewID { state.onReorder(tab.id, tabs[index + 1].id) }
                    }
            }
        }
        .frame(height: 50, alignment: .top)
    }
}

/// CodexBar's StackedToggleButton, B+ look: icon over title, a white tile when selected, a 2 pt usage bar at the bottom.
struct ProviderTile: View {
    let tab: TabModel
    let isSelected: Bool
    var show: PanelPrefs.Show = .used
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            VStack(spacing: 3) {
                if let logo = tab.logo { ProviderMark(logo: logo, size: 17) }
                else if tab.id == Presenter.overviewID {
                    Image(systemName: "square.grid.2x2").font(.system(size: 15, weight: .regular)).frame(height: 17)
                } else {
                    Image(systemName: "ellipsis").font(.system(size: 15, weight: .semibold)).frame(height: 17)
                }
                Text(tab.title).font(CF.face(10, isSelected ? .semibold : .medium)).lineLimit(1).fixedSize()
            }
            .foregroundStyle(isSelected ? CF.machineText : CF.quiet)
            .padding(.horizontal, 3).padding(.top, 6).frame(height: 44, alignment: .top)
            .background(RoundedRectangle(cornerRadius: 6).fill(isSelected ? CF.tile : (hovering ? CF.hover : Color.clear)))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(isSelected ? CF.tileEdge : Color.clear, lineWidth: 0.5))
            .background(GeometryReader { geo in
                Color.clear.preference(key: TileFrames.self, value: [tab.id: geo.frame(in: .named(MenuMetrics.space))])
            })
            .overlay(alignment: .bottom) {
                QuotaTick(tab: tab, show: show).padding(.horizontal, 6).padding(.bottom, 3)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityLabel(tab.id == "agy" ? "Google AI Pro (Antigravity)" : tab.title)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

struct ControlFrames: PreferenceKey {
    static var defaultValue: [MenuControl: CGRect] = [:]
    static func reduce(value: inout [MenuControl: CGRect], nextValue: () -> [MenuControl: CGRect]) {
        value.merge(nextValue()) { $1 }
    }
}

extension View {
    /// Registers this view's frame so the host can route a click on it (see MenuControl).
    func routed(_ control: MenuControl) -> some View {
        background(GeometryReader { geo in
            Color.clear.preference(key: ControlFrames.self, value: [control: geo.frame(in: .named(MenuMetrics.space))])
        })
    }
}

struct TileFrames: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue()) { $1 }
    }
}

/// The cross-provider status cue under each tab: how much of the tightest window is used (or left), terracotta when hot.
struct QuotaTick: View {
    let tab: TabModel
    var show: PanelPrefs.Show = .used

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(CF.track)
                if let used = tab.used {
                    let value = show == .left ? 100 - used : used
                    if value > 0 {
                        Capsule().fill(tab.hot ? CF.human : CF.machine)
                            .frame(width: max(2, geo.size.width * min(1, value / 100)))
                    }
                }
            }
        }
        .frame(height: 2)
    }
}

/// A quiet icon in the page header. Routed by position like every other control in the open menu.
struct HeaderButton: View {
    let symbol: String
    let label: String
    let control: MenuControl
    @ObservedObject var state: MenuState
    @State private var hovering = false

    var body: some View {
        Button { state.onControl(control) } label: {
            Image(systemName: symbol).font(.system(size: 12, weight: .regular))
                .foregroundStyle(hovering ? CF.ink : CF.quiet)
                .frame(width: 26, height: 22)
                .background(RoundedRectangle(cornerRadius: 5).fill(hovering ? CF.hover : Color.clear))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain).routed(control).onHover { hovering = $0 }
        .accessibilityLabel(label)
    }
}

struct ActionRow: View {
    let symbol: String?
    let title: String
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                if let symbol {
                    Image(systemName: symbol).font(.system(size: 12, weight: .regular)).frame(width: 16)
                }
                Text(title).font(CF.face(13)).lineLimit(1)
                Spacer(minLength: 0)
            }
            .foregroundStyle(hovering ? Color.white : CF.ink)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(RoundedRectangle(cornerRadius: 5).fill(hovering ? CF.machine : Color.clear))
            .padding(.horizontal, 6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}

struct EmptyCard: View {
    let text: String
    var body: some View {
        Text(text).font(CF.face(13)).foregroundStyle(CF.quiet)
            .padding(.horizontal, MenuMetrics.horizontal).padding(.vertical, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct MenuDivider: View {
    var inset = false
    var body: some View {
        Rectangle().fill(CF.rule).frame(height: 1)
            .padding(.horizontal, inset ? MenuMetrics.horizontal : 0)
    }
}

/// Words and figures in the system face; figures take tabular digits so columns line up.
struct Measure: View {
    let text: String
    let size: CGFloat
    let weight: CF.Weight

    init(_ text: String, size: CGFloat, weight: CF.Weight = .regular) {
        self.text = text; self.size = size; self.weight = weight
    }

    var body: some View {
        Text(text).font(CF.face(size, weight)).monospacedDigit()
    }
}
