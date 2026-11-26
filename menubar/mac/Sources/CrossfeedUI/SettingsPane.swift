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
        let prefs = state.prefs
        VStack(alignment: .leading, spacing: 0) {
            Button { state.onControl(.back) } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.left").font(.system(size: 12, weight: .semibold))
                    Text("Menu bar").font(CF.face(16, .semibold))
                    Spacer(minLength: 0)
                }
                .frame(minHeight: 28).contentShape(Rectangle())
            }
            .buttonStyle(.plain).routed(.back).accessibilityLabel("Back")
            .padding(.horizontal, MenuMetrics.horizontal).padding(.top, 10)

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
                }
                SettingRow("Resets") {
                    Chip("Countdown", selected: prefs.reset == .countdown, control: .resets(.countdown), state: state)
                    Chip("Clock", selected: prefs.reset == .clock, control: .resets(.clock), state: state)
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
