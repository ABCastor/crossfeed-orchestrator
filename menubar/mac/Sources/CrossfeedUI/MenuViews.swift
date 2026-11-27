// Crossfeed menu: the views of the open menu.
//
// Layout, spacing and the sequence of regions follow CodexBar's menu (MIT, Copyright (c) 2026 Peter
// Steinberger): ProviderSwitcherButtons / StatusItemController+SwitcherViews (tiles with a mini usage
// bar), UsageMenuCardView + UsageMenuCardHeaderAndUsageSectionView (header, labelled usage rows),
// MenuCardMetricRow (title, bar, "% used" and reset line), UsagePaceText (the pace line) and the
// footer action rows. Rewritten for Crossfeed's glance JSON and restyled with the console's palette.
import AppKit
import CrossfeedCore
import SwiftUI

/// Every button the host routes by position, because a click inside an open NSMenu reaches the hosting view, not SwiftUI.
public enum MenuControl: Hashable {
    case refresh, settings, back, reset, notifyLow, notifyReset, overviewHidden
    case openTab(String), usage(String?), toggleTab(String), moveTab(String, Int), settingsPage(Int)
    case show(String), window(String), count(BarPrefs.Count), icon(BarPrefs.Icon), text(BarPrefs.Text), extra(BarPrefs.Extra), resets(BarPrefs.Reset), refreshEvery(Int)
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
    /// Which half of the settings pane is open: 0 the menu bar item, 1 the tabs.
    @Published public var settingsPage = 0
    /// Which provider tabs are shown, and whether hidden ones still count in Overview.
    @Published public var layout = TabLayout()
    @Published public var isReloading = false
    /// One height for every tab and the settings pane, set by the host so the panel never jumps; the content scrolls inside.
    /// Nil sizes the menu to its content (what the height is measured from).
    @Published public var panelHeight: CGFloat?
    public var onControl: (MenuControl) -> Void = { _ in }

    /// A short crossfade and a few points of slide; none when the system asks for less motion.
    public static var animation: Animation? {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion ? nil : .easeOut(duration: 0.18)
    }

    /// Switch provider tab. The card fades and slides in; the panel keeps its height.
    public func choose(_ id: String) {
        guard id != current else { return }
        withAnimation(Self.animation) { selected = id }
    }

    /// The tab on screen: Overview unless a provider tab that is still shown was chosen.
    public var current: String {
        guard let selected, selected != Presenter.overviewID, layout.isVisible(selected),
              orderedTabs.contains(where: { $0.id == selected }) else { return Presenter.overviewID }
        return selected
    }

    /// Overview, then the provider tabs that are shown.
    public var shownTabs: [TabModel] {
        [TabModel(id: Presenter.overviewID, title: "Overview", logo: nil, used: nil, pace: .unknown, hot: false)]
            + orderedTabs.filter { layout.isVisible($0.id) }
    }
    public var canEditPolicy = true
    public var onPolicyChange: (PolicyChange) -> Void = { _ in }
    public var onSelect: (String) -> Void = { _ in }
    public var onReorder: (String, String) -> Void = { _, _ in }
    @Published public var providerOrder: [String] = []
    @Published var draggedTile: String?
    @Published var dropTile: String?
    private var pressedTile: (id: String, origin: CGPoint)?
    public var onOpenStatus: (URL) -> Void = { _ in }
    public var onOpenConsole: () -> Void = {}
    public var onReload: () -> Void = {}
    public var onAbout: () -> Void = {}
    public var onQuit: () -> Void = {}
    /// Tile rectangles in the menu view's own coordinates, so the host can route a click that the
    /// menu's tracking loop delivers to the view instead of to SwiftUI's button.
    public var tileFrames: [String: CGRect] = [:]
    public var policyFrames: [PolicyChange: CGRect] = [:]
    public var statusFrames: [String: StatusTarget] = [:]
    public var controlFrames: [MenuControl: CGRect] = [:]

    public var orderedTabs: [TabModel] {
        guard let tabs = presenter?.tabs else { return [] }
        let byID = Dictionary(uniqueKeysWithValues: tabs.map { ($0.id, $0) })
        let ordered = providerOrder.compactMap { byID[$0] }
        let used = Set(ordered.map(\.id))
        return ordered + tabs.filter { !used.contains($0.id) }
    }

    public func ownsPointer(at point: CGPoint) -> Bool {
        tile(at: point) != nil || policyFrames.values.contains { $0.contains(point) }
            || controlFrames.values.contains { $0.contains(point) }
            || statusFrames.values.contains { $0.frame.contains(point) }
    }

    public func beginPointer(at point: CGPoint) {
        cancelPointer()
        pressedTile = tile(at: point).map { ($0, point) }
    }

    public func movePointer(to point: CGPoint) {
        guard let press = pressedTile else { return }
        if draggedTile == nil {
            guard press.id != Presenter.overviewID else { return }
            guard hypot(point.x - press.origin.x, point.y - press.origin.y) >= 5 else { return }
            draggedTile = press.id
        }
        let target = tile(at: point)
        if dropTile != target { dropTile = target }
    }

    @discardableResult
    public func endPointer(at point: CGPoint) -> Bool {
        movePointer(to: point)
        defer { cancelPointer() }
        if let source = draggedTile {
            if let target = tile(at: point), target != source, target != Presenter.overviewID { onReorder(source, target) }
            return true // a drag cannot select a tab or move a dial
        }
        return routeClick(at: point)
    }

    public func cancelPointer() {
        pressedTile = nil
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
        if let hit = statusFrames.values.first(where: { $0.frame.contains(point) }) {
            onOpenStatus(hit.url)
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
    /// The live menu draws Open console, Reload, About and Quit as native menu items, as CodexBar does,
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
        .onPreferenceChange(StatusFrames.self) { state.statusFrames = $0 }
        .onPreferenceChange(ControlFrames.self) { state.controlFrames = $0 }
        .background(CF.paper.opacity(0.9))
        .foregroundStyle(CF.ink)
    }

    @ViewBuilder private var panes: some View {
            if state.showsSettings {
                scrolling { SettingsPane(state: state) }
            } else if let presenter = state.presenter {
                ProviderSwitcher(state: state)
                    .padding(.horizontal, 6).padding(.top, 8).padding(.bottom, 6)
                MenuDivider()
                scrolling {
                    VStack(alignment: .leading, spacing: 0) {
                        let tab = state.current
                        ZStack(alignment: .topLeading) {
                            if tab == Presenter.overviewID {
                                OverviewView(presenter: presenter, state: state)
                                    .id(tab)
                                    .transition(.asymmetric(insertion: .opacity.combined(with: .offset(y: 5)), removal: .opacity))
                            } else if let card = presenter.card(for: tab) {
                                UsageCard(card: card, state: state, now: presenter.now).padding(.top, 10)
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
            MenuDivider(inset: true)
            Text(failure).font(CF.face(11)).foregroundStyle(CF.humanText)
                .padding(.horizontal, MenuMetrics.horizontal).padding(.vertical, 6)
        }
    }
}

// MARK: Provider switcher

struct ProviderSwitcher: View {
    @ObservedObject var state: MenuState

    var body: some View {
        let tabs = state.shownTabs
        let source = tabs.firstIndex { $0.id == state.draggedTile }
        HStack(spacing: 1) {
            ForEach(Array(tabs.enumerated()), id: \.element.id) { index, tab in
                ProviderTile(tab: tab, isSelected: tab.id == state.current) { state.onSelect(tab.id) }
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
    }
}

/// CodexBar's StackedToggleButton: icon over title, filled when selected, a 2 pt usage bar at the bottom.
struct ProviderTile: View {
    let tab: TabModel
    let isSelected: Bool
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            VStack(spacing: 2) {
                if let logo = tab.logo { ProviderMark(logo: logo, size: 17) }
                else if tab.id == Presenter.overviewID {
                    // Icon only, so the strip keeps room for every provider's name.
                    Image(systemName: "square.grid.2x2").font(.system(size: 15, weight: .regular)).frame(height: 17)
                }
                if tab.id != Presenter.overviewID {
                    Text(tab.title).font(CF.face(10.5, isSelected ? .semibold : .medium)).lineLimit(1).minimumScaleFactor(0.7)
                }
            }
            .foregroundStyle(isSelected ? CF.machineText : CF.quiet)
            .frame(maxWidth: tab.id == Presenter.overviewID ? 32 : .infinity, minHeight: 30).padding(.top, 5).padding(.bottom, 9)
            .background(RoundedRectangle(cornerRadius: 6)
                .fill(isSelected ? CF.machine.opacity(0.13) : (hovering ? CF.ink.opacity(0.07) : Color.clear)))
            .background(GeometryReader { geo in
                Color.clear.preference(key: TileFrames.self, value: [tab.id: geo.frame(in: .named(MenuMetrics.space))])
            })
            .overlay(alignment: .bottom) {
                if tab.id != Presenter.overviewID { QuotaTick(tab: tab, onFill: isSelected).padding(.horizontal, 5).padding(.bottom, 3) }
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

/// The cross-provider status cue under each tile: how much of the tightest window is used, terracotta when hot.
struct QuotaTick: View {
    let tab: TabModel
    let onFill: Bool

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(onFill ? CF.machine.opacity(0.28) : CF.ink.opacity(0.16))
                if let used = tab.used, used > 0 {
                    Capsule().fill(tab.hot ? CF.human : CF.machine)
                        .frame(width: max(2, geo.size.width * min(1, used / 100)))
                }
            }
        }
        .frame(height: 2)
    }
}

// MARK: Card

struct UsageCard: View {
    let card: CardModel
    @ObservedObject var state: MenuState
    let now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text(card.heading).font(CF.face(16, .semibold)).lineLimit(1).layoutPriority(1)
                    Spacer(minLength: 4)
                    if let badge = card.badge { StateBadge(text: badge) }
                }
                HStack(spacing: 0) {
                    Text(state.isReloading ? "Updating…" : (card.isAntigravity ? "Antigravity · " + card.subtitle : card.subtitle))
                        .font(CF.face(11)).foregroundStyle(CF.quiet).lineLimit(1)
                    Spacer(minLength: 4)
                    HeaderButton(symbol: "arrow.clockwise", label: "Refresh now", control: .refresh, state: state)
                    HeaderButton(symbol: "slider.horizontal.3", label: "Menu bar settings", control: .settings, state: state)
                }
            }
            .padding(.horizontal, MenuMetrics.horizontal).padding(.bottom, 16)
            ForEach(Array(card.allowanceSections.enumerated()), id: \.element.id) { index, section in
                if index > 0 { MenuDivider(inset: true).padding(.vertical, 10) }
                SectionView(section: section, state: state, now: now, isAllowance: card.isAntigravity)
            }
            if let policy = card.subscriptionPolicy {
                MenuDivider(inset: true).padding(.vertical, 12)
                PolicyControl(policy: policy, state: state, now: now)
                    .padding(.horizontal, MenuMetrics.horizontal)
            }
            // Cost and usage by model live on the console; one quiet row goes there.
            LinkRow(title: "Usage and cost", control: .usage(card.usagePool), state: state)
                .padding(.top, 10)
        }
        .padding(.bottom, 8)
    }
}

/// A quiet icon in the card header. Routed by position like every other control in the open menu.
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
                .background(RoundedRectangle(cornerRadius: 5).fill(hovering ? CF.ink.opacity(0.07) : Color.clear))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain).routed(control).onHover { hovering = $0 }
        .accessibilityLabel(label)
    }
}

struct StateBadge: View {
    let text: String

    /// Conserve / Not read read as a hand on the dial (terracotta); abundant reads as unspent allowance (olive).
    private var color: Color {
        switch text.lowercased() {
        case "conserve", "scarce", "exhausted": return CF.humanText
        case "abundant": return CF.between
        default: return CF.quiet
        }
    }

    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(color).frame(width: 6, height: 6)
            Text(text).font(CF.face(11, .medium)).foregroundStyle(color).lineLimit(1)
        }
    }
}

struct SectionView: View {
    let section: SectionModel
    @ObservedObject var state: MenuState
    let now: Date
    let isAllowance: Bool

    /// Every window unread: the reason joins the "Not read" line instead of standing under it.
    private var mergesNote: Bool { !section.rows.isEmpty && section.rows.allSatisfy { $0.bar == nil && !$0.inactive } }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            if let title = section.title {
                HStack(spacing: 6) {
                    if let logo = section.logo { ProviderMark(logo: logo, size: 13) }
                    Text(title).font(CF.face(14, .semibold)).lineLimit(1)
                    Spacer(minLength: 4)
                    if let badge = section.badge { StateBadge(text: badge).layoutPriority(1) }
                }
            }
            VStack(alignment: .leading, spacing: 14) {
                ForEach(section.rows) { row in
                    // A window with nothing to draw says "Not read" and why on one line, not two.
                    var shown = row
                    if row.bar == nil, let note = section.note, mergesNote {
                        shown.usedText = row.usedText + " · " + note
                    }
                    return MetricRow(row: shown)
                }
            }
            if let note = section.note, !mergesNote {
                Text(isAllowance && note.hasPrefix("Last reading kept") ? "Last reading kept · refresh unavailable" : note)
                    .font(CF.face(11)).foregroundStyle(CF.quiet)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
            if isAllowance, !section.models.isEmpty {
                Text(section.models.joined(separator: " · ")).font(CF.face(11)).foregroundStyle(CF.quiet)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel("Models: " + section.models.joined(separator: ", "))
            }
            if let policy = section.policy {
                PolicyControl(policy: policy, state: state, now: now).padding(.top, 8)
            }
            ProviderExtrasView(extras: section.extras, id: section.id, openStatus: state.onOpenStatus)
        }
        .padding(.horizontal, MenuMetrics.horizontal).padding(.top, 4)
    }
}

/// A quiet row that opens something on the console: a label and an arrow, no colour until it is pointed at.
struct LinkRow: View {
    let title: String
    let control: MenuControl
    @ObservedObject var state: MenuState
    @State private var hovering = false

    var body: some View {
        Button { state.onControl(control) } label: {
            HStack(spacing: 6) {
                Text(title).font(CF.face(12, .medium))
                Image(systemName: "arrow.up.right").font(.system(size: 10, weight: .medium))
                Spacer(minLength: 0)
            }
            .foregroundStyle(hovering ? CF.ink : CF.quiet)
            .frame(minHeight: 26, alignment: .leading)
            .padding(.horizontal, MenuMetrics.horizontal)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain).routed(control).onHover { hovering = $0 }
        .accessibilityAddTraits(.isLink)
    }
}

public struct StatusTarget: Equatable {
    public let url: URL
    public let frame: CGRect
}

struct StatusFrames: PreferenceKey {
    static var defaultValue: [String: StatusTarget] = [:]
    static func reduce(value: inout [String: StatusTarget], nextValue: () -> [String: StatusTarget]) {
        value.merge(nextValue()) { $1 }
    }
}

struct ProviderExtrasView: View {
    let extras: ProviderExtras
    let id: String
    let openStatus: (URL) -> Void
    @State private var hoveringStatus = false

    var body: some View {
        if !extras.credits.isEmpty || extras.status != nil
            || extras.incident != nil || !extras.billing.isEmpty || !extras.modelQuota.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                if let incident = extras.incident {
                    // The word carries the level; terracotta only for a major incident or outage.
                    (Text(incident.title).font(CF.face(11.5, .semibold)).foregroundColor(incident.isSevere ? CF.humanText : CF.ink)
                        + Text(incident.detail.isEmpty ? "" : " · " + incident.detail).font(CF.face(11.5)).foregroundColor(CF.ink))
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityLabel("\(incident.title). \(incident.detail)")
                }
                if !extras.billing.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Subscription").font(CF.face(11.5, .medium)).foregroundStyle(CF.quiet)
                        ForEach(extras.billing, id: \.self) { line in
                            Measure(line, size: 11.5).foregroundStyle(CF.quiet)
                        }
                    }
                }
                if !extras.modelQuota.isEmpty { extraRows("Per-model allowance", rows: extras.modelQuota, stacked: true) }
                if !extras.creditRows.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(extras.creditRows.indices, id: \.self) { index in
                            let row = extras.creditRows[index]
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Measure(row.label, size: 12.5, weight: .medium).lineLimit(1)
                                Spacer(minLength: 4)
                                Measure(row.detail, size: 11.5).foregroundStyle(CF.quiet).lineLimit(1).fixedSize()
                            }
                            .accessibilityElement(children: .combine)
                        }
                    }
                }
                if let url = extras.status {
                    Button { openStatus(url) } label: {
                        Label("Status page", systemImage: "arrow.up.right.square")
                            .font(CF.face(11.5, .medium))
                            .foregroundStyle(hoveringStatus ? CF.ink : CF.machineText)
                            .frame(minHeight: 23, alignment: .leading)
                            .contentShape(Rectangle())
                            .background(GeometryReader { geo in
                                Color.clear.preference(key: StatusFrames.self, value: [id: StatusTarget(
                                    url: url, frame: geo.frame(in: .named(MenuMetrics.space)))])
                            })
                    }
                    .buttonStyle(.plain)
                    .onHover { hoveringStatus = $0 }
                    .accessibilityAddTraits(.isLink)
                }
            }
            .padding(.top, 6)
        }
    }

    private func extraRows(_ heading: String, rows: [ExtraRow], stacked: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(heading).font(CF.face(11.5, .medium)).foregroundStyle(CF.quiet)
            ForEach(rows.indices, id: \.self) { index in
                let row = rows[index]
                if stacked {
                    // Long details (a percent, a reset and a shared pool) wrap under the name instead of leaving the menu.
                    VStack(alignment: .leading, spacing: 1) {
                        Text(row.label).font(CF.face(11.5))
                        Measure(row.detail, size: 11.5).foregroundStyle(CF.quiet)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                } else {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(row.label).font(CF.face(11.5)).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                        Measure(row.detail, size: 11.5).foregroundStyle(CF.quiet)
                            .fixedSize(horizontal: true, vertical: false)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
    }
}

/// One allowance window in three lines: its name and when it resets, the bar, then how much is used
/// beside the pace. (CodexBar's MetricRow spends a fourth line on the pace.)
struct MetricRow: View {
    let row: RowModel

    var body: some View {
        if row.inactive {
            // A window the plan has but that is not running: one line, no bar.
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(row.title).font(CF.face(13, .medium)).lineLimit(1)
                Spacer(minLength: 4)
                if let note = row.resetText { Text(note).font(CF.face(11.5)).foregroundStyle(CF.quiet).lineLimit(1) }
            }
            .accessibilityElement(children: .combine)
        } else {
            running
        }
    }

    private var running: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(row.title).font(CF.face(13, .medium)).lineLimit(1)
                Spacer(minLength: 4)
                if let reset = row.resetText {
                    Measure(reset, size: 11.5).foregroundStyle(CF.quiet).lineLimit(1).fixedSize()
                }
            }
            if let bar = row.bar {
                UsageProgressBar(percent: bar.used, tint: bar.pace == .unknown && bar.hot ? CF.human : CF.machine,
                                 accessibilityLabel: row.title, pacePercent: bar.expected,
                                 paceOnTop: bar.pace != .deficit, paceColor: CF.paceColor(bar.pace))
            }
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if row.bar != nil {
                    Measure(row.usedText, size: 11.5, weight: .medium).fixedSize()
                } else {
                    Text(row.usedText).font(CF.face(11)).foregroundStyle(CF.quiet)
                }
                Spacer(minLength: 4)
                if let pace = row.paceText, let bar = row.bar {
                    HStack(spacing: 5) {
                        Circle().fill(CF.dotColor(bar.pace)).frame(width: 6, height: 6)
                        Text(pace).font(CF.face(11)).foregroundStyle(bar.pace == .deficit ? CF.humanText : CF.quiet)
                            .lineLimit(1).fixedSize()
                    }
                    .accessibilityElement(children: .combine).accessibilityLabel("Pace: " + pace)
                }
            }
        }
    }
}

// MARK: Spend and actions

/// The month's subscriptions: each plan with its price, then the total and the operator's share as quiet lines.
struct SpendCard: View {
    let model: SpendModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(model.title).font(CF.face(11.5, .medium)).foregroundStyle(CF.quiet)
            if model.plans.isEmpty {
                ForEach(model.lines, id: \.self) { line in Measure(line, size: 12) }
            } else {
                ForEach(Array(model.plans.enumerated()), id: \.offset) { _, plan in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(plan.name).font(CF.face(12.5, .medium)).lineLimit(1)
                        Spacer(minLength: 4)
                        Measure(plan.price, size: 11.5).foregroundStyle(plan.priced ? CF.ink : CF.quiet).lineLimit(1).fixedSize()
                    }
                    .accessibilityElement(children: .combine)
                }
                if !model.lines.isEmpty {
                    ForEach(model.lines, id: \.self) { line in Measure(line, size: 11.5).foregroundStyle(CF.quiet) }
                }
            }
            if let share = model.share, model.plans.isEmpty {
                Measure("Your share \(share.left) · \(share.right)", size: 11.5).foregroundStyle(CF.quiet)
            }
            if let note = model.note {
                Text(note).font(CF.face(11)).foregroundStyle(CF.quiet)
            }
        }
        .padding(.horizontal, MenuMetrics.horizontal).padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
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

/// Words in Commissioner, figures in Martian Mono: the console sets measurement in its machine face.
struct Measure: View {
    let text: String
    let size: CGFloat
    let weight: CF.Weight

    init(_ text: String, size: CGFloat, weight: CF.Weight = .regular) {
        self.text = text; self.size = size; self.weight = weight
    }

    var body: some View {
        text.split(separator: " ", omittingEmptySubsequences: false).enumerated().reduce(Text("")) { acc, part in
            let token = String(part.element)
            let figure = token.contains { $0.isNumber }
            let piece = Text(token).font(figure ? CF.machineFont(size - 2.5, weight == .regular ? .regular : .medium)
                                                : CF.face(size, weight))
            return acc + (part.offset == 0 ? Text("") : Text(" ").font(CF.face(size))) + piece
        }
    }
}
