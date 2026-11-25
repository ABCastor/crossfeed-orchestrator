// Crossfeed menu: the views of the open menu.
//
// Layout, spacing and the sequence of regions follow CodexBar's menu (MIT, Copyright (c) 2026 Peter
// Steinberger): ProviderSwitcherButtons / StatusItemController+SwitcherViews (tiles with a mini usage
// bar), UsageMenuCardView + UsageMenuCardHeaderAndUsageSectionView (header, labelled usage rows),
// MenuCardMetricRow (title, bar, "% used" and reset line), UsagePaceText (the pace line) and the
// footer action rows. Rewritten for Crossfeed's glance JSON and restyled with the console's palette.
import CrossfeedCore
import SwiftUI

/// What the menu needs from its host. All state lives here so a snapshot and the live menu share it.
public final class MenuState: ObservableObject {
    @Published public var presenter: Presenter?
    @Published public var selected: String?
    @Published public var failure: String?
    public var onSelect: (String) -> Void = { _ in }
    public var onOpenConsole: () -> Void = {}
    public var onReload: () -> Void = {}
    public var onAbout: () -> Void = {}
    public var onQuit: () -> Void = {}
    /// Tile rectangles in the menu view's own coordinates, so the host can route a click that the
    /// menu's tracking loop delivers to the view instead of to SwiftUI's button.
    public var tileFrames: [String: CGRect] = [:]

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
            if let presenter = state.presenter {
                ProviderSwitcher(tabs: presenter.tabs, selected: state.selected, onSelect: state.onSelect)
                    .padding(.horizontal, 6).padding(.top, 8).padding(.bottom, 6)
                MenuDivider()
                if let tab = state.selected, let card = presenter.card(for: tab) {
                    UsageCard(card: card).padding(.top, 6)
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
            if showsActions {
                MenuDivider(inset: true)
                VStack(alignment: .leading, spacing: 0) {
                    ActionRow(symbol: "arrow.up.right.square", title: "Open console", action: state.onOpenConsole)
                    ActionRow(symbol: "arrow.clockwise", title: "Reload view", action: state.onReload)
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
        .background(CF.paper.opacity(0.9))
        .foregroundStyle(CF.ink)
    }
}

// MARK: Provider switcher

struct ProviderSwitcher: View {
    let tabs: [TabModel]
    let selected: String?
    let onSelect: (String) -> Void

    var body: some View {
        HStack(spacing: 2) {
            ForEach(tabs) { tab in
                ProviderTile(tab: tab, isSelected: tab.id == selected) { onSelect(tab.id) }
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
                Image(systemName: tab.symbol).font(.system(size: 14, weight: .medium)).frame(height: 17)
                Text(tab.title).font(CF.face(10.5, .medium)).lineLimit(1).minimumScaleFactor(0.8)
            }
            .foregroundStyle(isSelected ? Color.white : CF.quiet)
            .frame(maxWidth: .infinity).padding(.top, 5).padding(.bottom, 9)
            .background(RoundedRectangle(cornerRadius: 6)
                .fill(isSelected ? CF.machine : (hovering ? CF.ink.opacity(0.07) : Color.clear)))
            .background(GeometryReader { geo in
                Color.clear.preference(key: TileFrames.self, value: [tab.id: geo.frame(in: .named(MenuMetrics.space))])
            })
            .overlay(alignment: .bottom) { QuotaTick(tab: tab, onFill: isSelected).padding(.horizontal, 5).padding(.bottom, 3) }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityLabel(tab.title)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
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
                Capsule().fill(onFill ? Color.white.opacity(0.28) : CF.ink.opacity(0.16))
                if let used = tab.used, used > 0 {
                    Capsule().fill(onFill ? Color.white : (tab.hot ? CF.human : CF.machine))
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

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text(card.title).font(CF.face(16, .semibold)).lineLimit(1).layoutPriority(1)
                    Spacer(minLength: 4)
                    if let badge = card.badge { StateBadge(text: badge) }
                }
                Text(card.subtitle).font(CF.face(11)).foregroundStyle(CF.quiet).lineLimit(1)
            }
            .padding(.horizontal, MenuMetrics.horizontal).padding(.bottom, 8)
            MenuDivider(inset: true).padding(.bottom, 6)
            ForEach(Array(card.sections.enumerated()), id: \.element.id) { index, section in
                if index > 0 { MenuDivider(inset: true).padding(.vertical, 4) }
                SectionView(section: section, multi: card.sections.count > 1)
            }
        }
        .padding(.bottom, 6)
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
    let multi: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: section.rows.isEmpty ? 4 : 14) {
            if let title = section.title {
                HStack(alignment: .firstTextBaseline) {
                    Text(title).font(CF.face(14, .semibold)).lineLimit(1).truncationMode(.tail).layoutPriority(0)
                    Spacer(minLength: 4)
                    if let badge = section.badge { StateBadge(text: badge).layoutPriority(1) }
                }
            }
            ForEach(section.rows) { row in MetricRow(row: row) }
            if let note = section.note {
                Text(note).font(CF.face(11)).foregroundStyle(CF.quiet)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, MenuMetrics.horizontal).padding(.top, 4)
    }
}

/// CodexBar's MetricRow: medium title, 6 pt bar, "% used" left and reset countdown right, pace line below.
struct MetricRow: View {
    let row: RowModel

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(row.title).font(CF.face(13, .medium)).lineLimit(1)
            if let bar = row.bar {
                UsageProgressBar(percent: bar.used, tint: bar.pace == .unknown && bar.hot ? CF.human : CF.machine,
                                 accessibilityLabel: row.title, pacePercent: bar.expected,
                                 paceOnTop: bar.pace != .deficit, paceColor: CF.paceColor(bar.pace))
                HStack(alignment: .firstTextBaseline) {
                    Measure(row.usedText, size: 11.5, weight: .medium)
                    Spacer(minLength: 8)
                    if let reset = row.resetText {
                        Measure(reset, size: 11.5).foregroundStyle(CF.quiet).lineLimit(1)
                    }
                }
                if let pace = row.paceText, let bar = row.bar {
                    HStack(spacing: 5) {
                        Circle().fill(CF.dotColor(bar.pace)).frame(width: 6, height: 6)
                        Text(pace).font(CF.face(11)).foregroundStyle(bar.pace == .deficit ? CF.humanText : CF.quiet)
                            .lineLimit(1).minimumScaleFactor(0.85)
                    }
                }
            } else {
                Text(row.usedText).font(CF.face(11)).foregroundStyle(CF.quiet)
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
            Text(model.title).font(CF.face(13, .medium))
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
            Text(model.title).font(CF.face(13, .medium))
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
