// Crossfeed menu: the menu bar settings pane. One live preview of the item as it will look, then five
// choices, each readable without opening anything. Same selected treatment as the provider tiles.
import AppKit
import CrossfeedCore
import SwiftUI

struct SettingsPane: View {
    @ObservedObject var state: MenuState

    private var summary: BarSummary {
        state.presenter?.barSummary(state.prefs)
            ?? BarSummary(text: BarPrefs.format(name: "Claude", window: "Week", used: 42, count: state.prefs.count, text: state.prefs.text),
                          left: 42, right: nil, deficit: false, tooltip: "", logo: .claude, icon: state.prefs.effective.icon,
                          name: "Claude", window: "Week", used: 42)
    }

    private var texts: [(BarPrefs.Text, String)] {
        let s = summary
        let used = s.used ?? 42
        return BarPrefs.Text.allCases.map { text in
            (text, text == .none ? "Off" : BarPrefs.format(name: s.name.isEmpty ? "Claude" : s.name,
                                                            window: s.window.isEmpty ? "Week" : s.window,
                                                            used: used, count: state.prefs.count, text: text))
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { state.onControl(.back) } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.left").font(.system(size: 12, weight: .semibold))
                    Text("Settings").font(CF.face(16, .semibold))
                    Spacer(minLength: 0)
                }
                .frame(minHeight: 28).contentShape(Rectangle())
            }
            .buttonStyle(.plain).routed(.back).accessibilityLabel("Back")
            .padding(.horizontal, MenuMetrics.horizontal).padding(.top, 10)

            HStack(spacing: 2) {
                Chip("Menu bar", selected: state.settingsPage == 0, control: .settingsPage(0), state: state)
                Chip("Tabs", selected: state.settingsPage == 1, control: .settingsPage(1), state: state)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, MenuMetrics.horizontal - 8).padding(.top, 6)

            if state.settingsPage == 1 {
                TabsEditor(state: state)
            } else {
                barOptions
            }
        }
    }

    @ViewBuilder private var barOptions: some View {
        let prefs = state.prefs
        VStack(alignment: .leading, spacing: 0) {
            Preview(summary: summary, deficit: summary.deficit)
                .padding(.horizontal, MenuMetrics.horizontal).padding(.top, 10)
            Text(summary.tooltip.split(separator: "\n").first.map(String.init) ?? "Nothing is read yet")
                .font(CF.face(11)).foregroundStyle(CF.quiet).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, MenuMetrics.horizontal).padding(.top, 6)

            MenuDivider(inset: true).padding(.vertical, 10)
            VStack(alignment: .leading, spacing: 10) {
                SettingRow("Show") {
                    Chip("Most pressed", selected: prefs.show == BarPrefs.pressed, control: .show(BarPrefs.pressed), state: state)
                    ForEach(state.presenter?.barChoices ?? [], id: \.id) { choice in
                        Chip(choice.title, selected: prefs.show == choice.id, control: .show(choice.id), state: state)
                    }
                }
                SettingRow("Window") {
                    ForEach(BarPrefs.windowChoices, id: \.self) { window in
                        Chip(window == BarPrefs.tightest ? "Tightest" : window, selected: prefs.window == window,
                             control: .window(window), state: state)
                    }
                }
                SettingRow("Count") {
                    Chip("Used", selected: prefs.count == .used, control: .count(.used), state: state)
                    Chip("Left", selected: prefs.count == .left, control: .count(.left), state: state)
                }
                SettingRow("Tanks") {
                    Text("Left 5-hour, right weekly.")
                        .font(CF.face(11)).foregroundStyle(CF.quiet).fixedSize(horizontal: false, vertical: true)
                }
                SettingRow("Icon") {
                    Chip("Tanks", selected: prefs.icon == .tanks, control: .icon(.tanks), state: state)
                    Chip("Logo", selected: prefs.icon == .logo, control: .icon(.logo), state: state)
                    Chip("Off", selected: prefs.icon == .none, control: .icon(.none), state: state)
                }
                SettingRow("Text") {
                    ForEach(texts, id: \.0) { text, sample in
                        Chip(sample, selected: prefs.text == text, control: .text(text), state: state)
                    }
                }
                SettingRow("Add") {
                    Chip("None", selected: prefs.extra == .none, control: .extra(.none), state: state)
                    Chip("Reset", selected: prefs.extra == .reset, control: .extra(.reset), state: state)
                    Chip("Pace", selected: prefs.extra == .pace, control: .extra(.pace), state: state)
                    Chip("Cost today", selected: prefs.extra == .cost, control: .extra(.cost), state: state)
                }
                SettingRow("Resets") {
                    Chip("Countdown", selected: prefs.reset == .countdown, control: .resets(.countdown), state: state)
                    Chip("Clock", selected: prefs.reset == .clock, control: .resets(.clock), state: state)
                }
                SettingRow("Notify") {
                    Chip("Running low", selected: prefs.notifyLow, control: .notifyLow, state: state)
                    Chip("Reset", selected: prefs.notifyReset, control: .notifyReset, state: state)
                }
                SettingRow("Refresh") {
                    ForEach(BarPrefs.refreshChoices, id: \.self) { seconds in
                        Chip("\(seconds / 60) min", selected: prefs.refreshSeconds == seconds, control: .refreshEvery(seconds), state: state)
                    }
                }
            }
            .padding(.horizontal, MenuMetrics.horizontal - 8)

            MenuDivider(inset: true).padding(.top, 10)
            Button { state.onControl(.reset) } label: {
                Text("Reset to defaults").font(CF.face(12, .medium)).foregroundStyle(CF.machineText)
                    .frame(minHeight: 28, alignment: .leading).contentShape(Rectangle())
            }
            .buttonStyle(.plain).routed(.reset)
            .padding(.horizontal, MenuMetrics.horizontal).padding(.bottom, 6)
        }
    }
}

/// Which provider tabs the menu shows, and in what order. A switch hides a tab; the arrows move it.
/// "Other" always stays last. Hidden providers count in Overview only when the operator turns that on.
struct TabsEditor: View {
    @ObservedObject var state: MenuState

    var body: some View {
        let tabs = state.orderedTabs
        let movable = tabs.filter { $0.id != "other" }
        VStack(alignment: .leading, spacing: 0) {
            Text("Show the providers you use. Overview is always first.")
                .font(CF.face(11)).foregroundStyle(CF.quiet).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, MenuMetrics.horizontal).padding(.top, 12).padding(.bottom, 6)
            VStack(spacing: 0) {
                ForEach(tabs) { tab in
                    let index = movable.firstIndex { $0.id == tab.id }
                    TabEditRow(tab: tab, shown: state.layout.isVisible(tab.id),
                               canUp: (index ?? 0) > 0, canDown: index.map { $0 + 1 < movable.count } ?? false, state: state)
                }
            }
            .padding(.horizontal, 6)
            MenuDivider(inset: true).padding(.vertical, 10)
            SwitchRow(title: "Count hidden in Overview", on: state.layout.hiddenInOverview, control: .overviewHidden, state: state)
                .padding(.horizontal, 6)
            Text("Hidden tabs stay out of the strip. Their rows can still show in Overview.")
                .font(CF.face(11)).foregroundStyle(CF.quiet).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, MenuMetrics.horizontal).padding(.top, 2).padding(.bottom, 10)
        }
    }
}

struct TabEditRow: View {
    let tab: TabModel
    let shown: Bool
    let canUp: Bool
    let canDown: Bool
    @ObservedObject var state: MenuState

    var body: some View {
        HStack(spacing: 10) {
            Group {
                if let logo = tab.logo { ProviderMark(logo: logo, size: 16) } else { Color.clear }
            }
            .frame(width: 16, height: 16).opacity(shown ? 1 : 0.4)
            Text(tab.title).font(CF.face(13, .medium)).foregroundStyle(shown ? CF.ink : CF.quiet).lineLimit(1)
            Spacer(minLength: 4)
            ArrowButton(symbol: "chevron.up", label: "Move \(tab.title) up", enabled: canUp, control: .moveTab(tab.id, -1), state: state)
            ArrowButton(symbol: "chevron.down", label: "Move \(tab.title) down", enabled: canDown, control: .moveTab(tab.id, 1), state: state)
            SwitchView(on: shown).frame(width: 44, height: 30).contentShape(Rectangle())
                .routed(.toggleTab(tab.id))
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Show \(tab.title)")
                .accessibilityValue(shown ? "on" : "off")
                .accessibilityAddTraits(.isButton)
        }
        .padding(.leading, 10).padding(.trailing, 4).frame(minHeight: 34)
    }
}

struct ArrowButton: View {
    let symbol: String
    let label: String
    let enabled: Bool
    let control: MenuControl
    @ObservedObject var state: MenuState
    @State private var hovering = false

    var body: some View {
        Image(systemName: symbol).font(.system(size: 10, weight: .semibold))
            .foregroundStyle(enabled ? (hovering ? CF.ink : CF.quiet) : CF.quiet.opacity(0.3))
            .frame(width: 24, height: 24)
            .background(RoundedRectangle(cornerRadius: 5).fill(hovering && enabled ? CF.ink.opacity(0.07) : Color.clear))
            .contentShape(Rectangle())
            .onHover { hovering = $0 }
            .routed(enabled ? control : .moveTab("", 0))
            .accessibilityElement(children: .ignore).accessibilityLabel(label)
    }
}

/// A switch drawn in the menu's own colours (the row it sits in takes the click).
struct SwitchView: View {
    let on: Bool
    var body: some View {
        ZStack(alignment: on ? .trailing : .leading) {
            Capsule().fill(on ? CF.machine : CF.ink.opacity(0.18)).frame(width: 30, height: 18)
            Circle().fill(Color.white).frame(width: 14, height: 14).padding(2)
        }
        .frame(width: 30, height: 18)
        .animation(MenuState.animation, value: on)
    }
}

struct SwitchRow: View {
    let title: String
    let on: Bool
    let control: MenuControl
    @ObservedObject var state: MenuState

    var body: some View {
        HStack {
            Text(title).font(CF.face(13, .medium))
            Spacer(minLength: 4)
            SwitchView(on: on).frame(width: 44, height: 30).contentShape(Rectangle()).routed(control)
                .accessibilityElement(children: .ignore).accessibilityLabel(title)
                .accessibilityValue(on ? "on" : "off").accessibilityAddTraits(.isButton)
        }
        .padding(.leading, 10).padding(.trailing, 4).frame(minHeight: 34)
    }
}

/// The item as it will sit in the bar: icon, then the text, on the menu's own surface.
struct Preview: View {
    let summary: BarSummary
    let deficit: Bool

    var body: some View {
        HStack(spacing: 5) {
            if let icon = MenuBarIcon.image(for: summary) {
                Image(nsImage: icon).renderingMode(icon.isTemplate ? .template : .original)
                    .foregroundStyle(deficit ? CF.human : CF.ink)
            }
            if !summary.text.isEmpty {
                Text(summary.text).font(.system(size: 12, weight: .medium).monospacedDigit())
                    .foregroundStyle(deficit ? CF.humanText : CF.ink)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12).frame(height: 32)
        .background(RoundedRectangle(cornerRadius: 8).fill(CF.surface))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(CF.rule, lineWidth: 1))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Menu bar preview")
        .accessibilityValue(summary.text)
    }
}

struct SettingRow<Content: View>: View {
    let label: String
    @ViewBuilder let content: Content

    init(_ label: String, @ViewBuilder content: () -> Content) {
        self.label = label
        self.content = content()
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 0) {
            Text(label).font(CF.face(11.5, .medium)).foregroundStyle(CF.quiet)
                .frame(width: 58, alignment: .leading).padding(.leading, 8)
            Flow(spacing: 2, rowSpacing: 2) { content }
        }
    }
}

struct Chip: View {
    let title: String
    let selected: Bool
    let control: MenuControl
    @ObservedObject var state: MenuState
    @State private var hovering = false

    init(_ title: String, selected: Bool, control: MenuControl, state: MenuState) {
        self.title = title; self.selected = selected; self.control = control; self.state = state
    }

    var body: some View {
        Button { state.onControl(control) } label: {
            Text(title).font(CF.face(12, selected ? .semibold : .medium)).lineLimit(1)
                .foregroundStyle(selected ? CF.machineText : CF.quiet)
                .padding(.horizontal, 8).frame(minHeight: 24)
                .background(RoundedRectangle(cornerRadius: 6)
                    .fill(selected ? CF.machine.opacity(0.13) : (hovering ? CF.ink.opacity(0.07) : Color.clear)))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain).routed(control).onHover { hovering = $0 }
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// Left to right, wrapping: the options of one setting sit on as few lines as fit.
struct Flow: Layout {
    var spacing: CGFloat
    var rowSpacing: CGFloat

    private func place(_ width: CGFloat, _ subviews: Subviews) -> (points: [CGPoint], size: CGSize) {
        var points: [CGPoint] = [], x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, widest: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > width { x = 0; y += rowHeight + rowSpacing; rowHeight = 0 }
            points.append(CGPoint(x: x, y: y))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
            widest = max(widest, x - spacing)
        }
        return (points, CGSize(width: widest, height: y + rowHeight))
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        place(proposal.width ?? 300, subviews).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let laid = place(bounds.width, subviews)
        for (view, point) in zip(subviews, laid.points) {
            view.place(at: CGPoint(x: bounds.minX + point.x, y: bounds.minY + point.y), proposal: .unspecified)
        }
    }
}
