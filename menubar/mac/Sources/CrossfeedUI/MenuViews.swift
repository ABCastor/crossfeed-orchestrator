// Crossfeed menu: the views of the open menu.
//
// Layout, spacing and the sequence of regions follow CodexBar's menu (MIT, Copyright (c) 2026 Peter
// Steinberger): ProviderSwitcherButtons / StatusItemController+SwitcherViews (tiles with a mini usage
// bar), UsageMenuCardView + UsageMenuCardHeaderAndUsageSectionView (header, labelled usage rows),
// MenuCardMetricRow (title, bar, "% used" and reset line), UsagePaceText (the pace line) and the
// footer action rows. Rewritten for Crossfeed's glance JSON and restyled with the console's palette.
import CrossfeedCore
import SwiftUI

/// Every button the host routes by position, because a click inside an open NSMenu reaches the hosting view, not SwiftUI.
public enum MenuControl: Hashable {
    case refresh, settings, back, reset
    case show(String), window(String), count(BarPrefs.Count), icon(BarPrefs.Icon), text(BarPrefs.Text), extra(BarPrefs.Extra), refreshEvery(Int)
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
    @Published public var isReloading = false
    public var onControl: (MenuControl) -> Void = { _ in }
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

    var orderedTabs: [TabModel] {
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
            if let target = tile(at: point), target != source { onReorder(source, target) }
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
        self.selected = selected ?? presenter?.defaultTab
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
            if state.showsSettings {
                SettingsPane(state: state)
            } else if let presenter = state.presenter {
                ProviderSwitcher(state: state)
                    .padding(.horizontal, 6).padding(.top, 8).padding(.bottom, 6)
                MenuDivider()
                if let tab = state.selected, let card = presenter.card(for: tab) {
                    UsageCard(card: card, state: state, now: presenter.now).padding(.top, 6)
                }
                MenuDivider(inset: true)
                if let share = presenter.spend.share {
                    ShareCard(model: share).padding(.top, 4)
                    MenuDivider(inset: true).padding(.top, 4)
                }
                SpendCard(model: presenter.spend).padding(.vertical, 4)
            } else {
                EmptyCard(text: state.failure ?? "Reading quotas…")
            }
            if let failure = state.actionFailure {
                MenuDivider(inset: true)
                Text(failure).font(CF.face(11)).foregroundStyle(CF.humanText)
                    .padding(.horizontal, MenuMetrics.horizontal).padding(.vertical, 6)
            }
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
        .frame(width: MenuMetrics.width, alignment: .leading)
        .coordinateSpace(name: MenuMetrics.space)
        .onPreferenceChange(TileFrames.self) { state.tileFrames = $0 }
        .onPreferenceChange(PolicyFrames.self) { state.policyFrames = $0 }
        .onPreferenceChange(StatusFrames.self) { state.statusFrames = $0 }
        .onPreferenceChange(ControlFrames.self) { state.controlFrames = $0 }
        .background(CF.paper.opacity(0.9))
        .foregroundStyle(CF.ink)
    }
}

// MARK: Provider switcher

struct ProviderSwitcher: View {
    @ObservedObject var state: MenuState

    var body: some View {
        let tabs = state.orderedTabs
        let source = tabs.firstIndex { $0.id == state.draggedTile }
        HStack(spacing: 2) {
            ForEach(Array(tabs.enumerated()), id: \.element.id) { index, tab in
                ProviderTile(tab: tab, isSelected: tab.id == state.selected) { state.onSelect(tab.id) }
                    .opacity(tab.id == state.draggedTile ? 0.5 : 1)
                    .overlay(alignment: (source ?? index) < index ? .trailing : .leading) {
                        if tab.id == state.dropTile && tab.id != state.draggedTile {
                            Capsule().fill(CF.machine).frame(width: 2).padding(.vertical, 4)
                                .allowsHitTesting(false).accessibilityHidden(true)
                        }
                    }
                    .accessibilityAction(named: "Move left") {
                        if index > 0 { state.onReorder(tab.id, tabs[index - 1].id) }
                    }
                    .accessibilityAction(named: "Move right") {
                        if index + 1 < tabs.count { state.onReorder(tab.id, tabs[index + 1].id) }
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
                Text(tab.title).font(CF.face(10.5, isSelected ? .semibold : .medium)).lineLimit(1).minimumScaleFactor(0.8)
            }
            .foregroundStyle(isSelected ? CF.machineText : CF.quiet)
            .frame(maxWidth: .infinity, minHeight: 30).padding(.top, 5).padding(.bottom, 9)
            .background(RoundedRectangle(cornerRadius: 6)
                .fill(isSelected ? CF.machine.opacity(0.13) : (hovering ? CF.ink.opacity(0.07) : Color.clear)))
            .background(GeometryReader { geo in
                Color.clear.preference(key: TileFrames.self, value: [tab.id: geo.frame(in: .named(MenuMetrics.space))])
            })
            .overlay(alignment: .bottom) { QuotaTick(tab: tab, onFill: isSelected).padding(.horizontal, 5).padding(.bottom, 3) }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help("\(tab.title) · Drag to reorder")
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
            .padding(.horizontal, MenuMetrics.horizontal).padding(.bottom, 12)
            ForEach(Array(card.allowanceSections.enumerated()), id: \.element.id) { index, section in
                if index > 0 { MenuDivider(inset: true).padding(.vertical, 5) }
                SectionView(section: section, state: state, now: now, isAllowance: card.isAntigravity)
            }
            if let policy = card.subscriptionPolicy {
                MenuDivider(inset: true).padding(.vertical, 7)
                PolicyControl(policy: policy, state: state, now: now)
                    .padding(.horizontal, MenuMetrics.horizontal)
            }
        }
        .padding(.bottom, 6)
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
        .help(label).accessibilityLabel(label)
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
    private var mergesNote: Bool { !section.rows.isEmpty && section.rows.allSatisfy { $0.bar == nil } }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            if let title = section.title {
                HStack(spacing: 6) {
                    if let logo = section.logo { ProviderMark(logo: logo, size: 13) }
                    Text(title).font(CF.face(14, .semibold)).lineLimit(1)
                    Spacer(minLength: 4)
                    if let badge = section.badge { StateBadge(text: badge).layoutPriority(1) }
                }
            }
            VStack(alignment: .leading, spacing: 9) {
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
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true).help(note)
            }
            if isAllowance, !section.models.isEmpty {
                Text(section.models.joined(separator: " · ")).font(CF.face(11)).foregroundStyle(CF.quiet)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel("Models: " + section.models.joined(separator: ", "))
            }
            if let policy = section.policy {
                PolicyControl(policy: policy, state: state, now: now)
            }
            ProviderExtrasView(extras: section.extras, id: section.id, openStatus: state.onOpenStatus)
        }
        .padding(.horizontal, MenuMetrics.horizontal).padding(.top, 4)
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
        if !extras.credits.isEmpty || !extras.usage.isEmpty || !extras.perModel.isEmpty || extras.status != nil
            || extras.incident != nil || !extras.billing.isEmpty || !extras.modelQuota.isEmpty || !extras.costByModel.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
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
                if !extras.credits.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(extras.creditTitle).font(CF.face(11.5, .medium)).foregroundStyle(CF.quiet)
                        ForEach(extras.credits, id: \.self) { line in
                            Measure(line, size: 11.5).foregroundStyle(CF.quiet)
                        }
                    }
                }
                if !extras.usage.isEmpty { extraRows("Usage cost estimate", rows: extras.usage) }
                if !extras.perModel.isEmpty { extraRows("Spend by model · last 30 days", rows: extras.perModel) }
                if !extras.costByModel.isEmpty { extraRows("Cost by model · last 30 days", rows: extras.costByModel) }
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
            .padding(.top, 4)
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

/// CodexBar's Extra usage card: a labelled bar, what it holds on the left and how much on the right.
struct ShareCard: View {
    let model: ShareModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(model.title).font(CF.face(11.5, .medium)).foregroundStyle(CF.quiet)
            ShareBar(share: model.fraction, label: model.title)
            HStack(alignment: .firstTextBaseline) {
                Measure(model.left, size: 11.5)
                Spacer(minLength: 8)
                Measure(model.right, size: 11.5).foregroundStyle(CF.quiet)
            }
        }
        .padding(.horizontal, MenuMetrics.horizontal).padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// CodexBar's Cost card: the month's subscriptions as plain lines.
struct SpendCard: View {
    let model: SpendModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(model.title).font(CF.face(11.5, .medium)).foregroundStyle(CF.quiet)
            ForEach(model.lines, id: \.self) { line in
                Measure(line, size: 11.5)
            }
            if let note = model.note {
                Text(note).font(CF.face(11)).foregroundStyle(CF.quiet)
            }
        }
        .padding(.horizontal, MenuMetrics.horizontal).padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct ShareBar: View {
    let share: Double
    let label: String
    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(CF.ink.opacity(0.14))
                Capsule().fill(CF.machine).frame(width: max(6, geo.size.width * share))
            }
        }
        .frame(height: 6)
        .accessibilityLabel(label)
        .accessibilityValue("\(Int((share * 100).rounded())) percent")
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
