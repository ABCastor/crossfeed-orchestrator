// Crossfeed menu: the menu bar settings pane. One live preview of the item as it will look, then five
// choices, each readable without opening anything. Same selected treatment as the provider tiles.
import AppKit
import CrossfeedCore
import SwiftUI

struct SettingsPane: View {
    @ObservedObject var state: MenuState

    private var summary: BarSummary {
        state.live?.barSummary(state.prefs)
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
                Chip("General", selected: state.settingsPage == 0, control: .settingsPage(0), state: state)
                Chip("Tiles", selected: state.settingsPage == 1, control: .settingsPage(1), state: state)
                Chip("Tabs", selected: state.settingsPage == 2, control: .settingsPage(2), state: state)
                Chip("Menu bar", selected: state.settingsPage == 3, control: .settingsPage(3), state: state)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, MenuMetrics.horizontal - 8).padding(.top, 6)

            switch state.settingsPage {
            case 1: TilesEditor(state: state)
            case 2: TabsEditor(state: state)
            case 3: barOptions
            default: GeneralSettings(state: state)
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
                SettingRow("Icon") {
                    Chip("Crossfeed", selected: prefs.icon == .tanks, control: .icon(.tanks), state: state)
                    Chip("Provider logo", selected: prefs.icon == .logo, control: .icon(.logo), state: state)
                    Chip("None", selected: prefs.icon == .none, control: .icon(.none), state: state)
                }
                SettingNote(prefs.icon == .tanks ? "Two tanks: the left is the 5-hour window, the right the weekly one."
                            : (prefs.icon == .logo ? "The logo of the provider the text is about: the tightest one."
                               : (prefs.text == .none ? "Nothing to draw: the Crossfeed icon comes back." : "Text only.")))
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
                if prefs.extra == .reset {
                    SettingRow("Resets") {
                        Chip("Countdown", selected: prefs.reset == .countdown, control: .resets(.countdown), state: state)
                        Chip("Clock", selected: prefs.reset == .clock, control: .resets(.clock), state: state)
                    }
                }
            }
            .padding(.horizontal, MenuMetrics.horizontal - 8)

            MenuDivider(inset: true).padding(.top, 10)
            Button { state.onControl(.reset) } label: {
                Text("Reset the menu bar to defaults").font(CF.face(12, .medium)).foregroundStyle(CF.machineText)
                    .frame(minHeight: 28, alignment: .leading).contentShape(Rectangle())
            }
            .buttonStyle(.plain).routed(.reset)
            .padding(.horizontal, MenuMetrics.horizontal).padding(.bottom, 6)
        }
    }
}

/// Settings, General: the look, which tab opens, how the numbers read, when the menu reads again, the notifications and
/// the monthly budget of each API pool.
struct GeneralSettings: View {
    @ObservedObject var state: MenuState

    /// The API pools that report money: only those have something a budget can be set against.
    private var apiTabs: [TabModel] { state.orderedTabs.filter { state.live?.isBudgetable($0.id) == true } }

    var body: some View {
        let panel = state.panel, prefs = state.prefs
        let every = Array(Set(PanelPrefs.refreshChoices + [panel.refreshEveryMinutes])).sorted()
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                SettingRow("Appearance") {
                    ForEach(PanelPrefs.Appearance.allCases, id: \.self) { look in
                        Chip(look.title, selected: panel.appearance == look, control: .appearance(look), state: state)
                    }
                }
                SettingRow("Resets") {
                    ForEach(PanelPrefs.AutoUseResets.allCases, id: \.self) { choice in
                        Chip(choice.title, selected: panel.autoUseResets == choice, control: .autoUseResets(choice), state: state)
                    }
                }
                SettingNote(panel.autoUseResets == .off ? "A held reset is only used when you press Use."
                            : "When a window is used up and its natural reset is over 12 hours away, one held reset is used, at most once per window every 12 hours.")
                SettingRow("Show") {
                    Chip("Used", selected: panel.show == .used, control: .panelShow(.used), state: state)
                    Chip("Left", selected: panel.show == .left, control: .panelShow(.left), state: state)
                }
                SettingRow("Open on") {
                    Chip("Last used", selected: panel.openOn == "last", control: .openOn("last"), state: state)
                    Chip("Overview", selected: panel.openOn == Presenter.overviewID, control: .openOn(Presenter.overviewID), state: state)
                    ForEach(state.shownTabs.filter { $0.id != Presenter.overviewID }) { tab in
                        Chip(tab.title, selected: panel.openOn == tab.id, control: .openOn(tab.id), state: state)
                    }
                }
                SettingRow("Refresh") {
                    Chip("When opened", selected: panel.refreshOnOpen, control: .refreshOnOpen, state: state)
                }
                SettingNote(panel.refreshOnOpen ? "Opening the menu shows what it has and reads again if that is over a minute old."
                            : "Opening the menu does not read again.")
                SettingRow("Every") {
                    ForEach(every, id: \.self) { minutes in
                        Chip(minutes == 0 ? "Off" : "\(minutes) min", selected: panel.refreshEveryMinutes == minutes, control: .refreshEvery(minutes), state: state)
                    }
                }
                SettingRow("Notify") {
                    Chip("Running low", selected: prefs.notifyLow, control: .notifyLow, state: state)
                    Chip("Reset", selected: prefs.notifyReset, control: .notifyReset, state: state)
                }
            }
            .padding(.horizontal, MenuMetrics.horizontal - 8).padding(.top, 10)
            if !apiTabs.isEmpty {
                MenuDivider(inset: true).padding(.vertical, 10)
                Text("Monthly budget").font(CF.face(12, .semibold)).foregroundStyle(CF.quiet)
                    .padding(.horizontal, MenuMetrics.horizontal).padding(.bottom, 6)
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(apiTabs) { tab in
                        let set = panel.budgets[tab.id]
                        let choices = Array(Set(PanelPrefs.budgetChoices + (set.map { [$0] } ?? []))).sorted()
                        VStack(alignment: .leading, spacing: 1) {
                            Text(tab.title).font(CF.face(11.5, .medium)).foregroundStyle(CF.quiet).padding(.leading, 8)
                            Flow(spacing: 2, rowSpacing: 2) {
                                Chip("None", selected: set == nil, control: .budget(tab.id, nil), state: state)
                                ForEach(choices, id: \.self) { usd in
                                    Chip(Presenter.usdPlain(usd), selected: set == usd, control: .budget(tab.id, usd), state: state)
                                }
                            }
                            if set == nil, let carried = state.live?.readingBudget(tab.id) {
                                Text("Uses the reading's \(Presenter.usdPlain(carried)) until you pick one.")
                                    .font(CF.face(11)).foregroundStyle(CF.quiet).padding(.leading, 8)
                            }
                        }
                    }
                }
                .padding(.horizontal, MenuMetrics.horizontal - 8)
                Text("The tab's bar shows spend against it. Nothing stops at it.")
                    .font(CF.face(11)).foregroundStyle(CF.quiet).fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, MenuMetrics.horizontal).padding(.top, 6)
            }
            Spacer(minLength: 0).frame(height: 12)
        }
    }
}

struct SettingNote: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text).font(CF.face(11)).foregroundStyle(CF.quiet).fixedSize(horizontal: false, vertical: true)
            .padding(.leading, 8 + SettingRow<EmptyView>.labelWidth).frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Which provider tabs the menu shows, and in what order. A switch hides a tab; the arrows move it (a drag in the strip too).
/// "Other" always stays last.
struct TabsEditor: View {
    @ObservedObject var state: MenuState

    var body: some View {
        let tabs = state.orderedTabs
        let movable = tabs.filter { $0.id != "other" }
        VStack(alignment: .leading, spacing: 0) {
            Text("Overview is always first.")
                .font(CF.face(11)).foregroundStyle(CF.quiet).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, MenuMetrics.horizontal).padding(.top, 12).padding(.bottom, 6)
            VStack(spacing: 0) {
                ForEach(tabs) { tab in
                    let index = movable.firstIndex { $0.id == tab.id }
                    TabEditRow(tab: tab, shown: state.panel.isTabVisible(tab.id),
                               canUp: (index ?? 0) > 0, canDown: index.map { $0 + 1 < movable.count } ?? false, state: state)
                }
            }
            .padding(.horizontal, 6).padding(.bottom, 10)
        }
    }
}

/// Used or Left, then one tab's tiles: on or off, and a grip to drag them into order. Add tile brings back what is off
/// and lists every plugin tile.
struct TilesEditor: View {
    @ObservedObject var state: MenuState

    private static let names = ["limits": "Limits", "resets": "Resets", "tokens": "Tokens", "policy": "Spend policy",
                                "credits": "Credits", "links": "Links", "alert": "Alert", "providers": "Providers",
                                "subscriptions": "Subscriptions", "status": "Status", "spend": "Spend", "balance": "Balance",
                                "models": "Models", "activity": "Activity"]

    private func name(_ id: String, plugins: [PluginEntry]) -> String {
        Self.names[id] ?? plugins.first { $0.id == id }?.doc.title ?? String(id.split(separator: "/").last ?? Substring(id))
    }

    var body: some View {
        let presenter = state.live
        let plugins = presenter?.glance.plugins ?? []
        let tab = state.settingsTab
        let overview = presenter?.overviewPage(order: [], show: state.panel.show)
        let builtIns: [String] = {
            if tab == Presenter.overviewID {
                return [TileID.alert, TileID.providers, TileID.links, TileID.status]
                    + (overview?.subscriptions != nil ? [TileID.subscriptions] : []) + (overview?.tokens != nil ? [TileID.tokens] : [])
            }
            return presenter?.providerPage(for: tab, show: state.panel.show)?.available ?? PanelPrefs.providerTiles
        }()
        // Overview's status tile is on by itself while a provider reports an incident.
        let auto = tab == Presenter.overviewID && overview?.incidents.isEmpty == false ? [TileID.status] : []
        let listed = state.panel.listed(tab, builtIns: builtIns, auto: auto)
        let on = listed.on.filter { $0 != TileID.alert }
        let alertOn = listed.on.contains(TileID.alert)
        let pluginCandidates = plugins.map(\.id).filter { !listed.on.contains($0) }
        let candidates = listed.off + builtIns.filter { !listed.on.contains($0) && !listed.off.contains($0) } + pluginCandidates
        let chart = PanelPrefs.chartTile(on: tab, available: tab == Presenter.overviewID ? (overview?.tokens != nil ? [TileID.tokens] : []) : builtIns)
            .flatMap { id in listed.on.contains(id) ? id : nil }
        let choice = chart.flatMap { state.chartOptions(tab: tab, tile: $0) }
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                SettingRow("Tab") {
                    ForEach(state.shownTabs) { tab in
                        Chip(tab.title, selected: state.settingsTab == tab.id, control: .settingsTab(tab.id), state: state)
                    }
                }
            }
            .padding(.horizontal, MenuMetrics.horizontal - 8).padding(.top, 10)
            MenuDivider(inset: true).padding(.vertical, 10)
            if tab == Presenter.overviewID {
                TileSwitchRow(title: name(TileID.alert, plugins: plugins), on: alertOn, control: .toggleTile(tab, TileID.alert), state: state)
                    .padding(.horizontal, 8).padding(.bottom, 4)
            }
            if on.isEmpty {
                Text("No tiles on.").font(CF.face(12)).foregroundStyle(CF.quiet).padding(.horizontal, MenuMetrics.horizontal).padding(.bottom, 6)
            } else {
                ReorderableTiles(tab: "settings:" + tab, tiles: on.map { id in
                    TileItem(id: id, content: AnyView(TileSwitchRow(title: name(id, plugins: plugins), on: true, grip: true,
                                                                   control: .toggleTile(tab, id), state: state)))
                }, state: state, gap: 4)
                .padding(.horizontal, 8)
            }
            Button { state.onControl(.addOpen) } label: {
                HStack(spacing: 6) {
                    Image(systemName: state.addOpen ? "minus" : "plus").font(.system(size: 11, weight: .semibold))
                    Text("Add tile").font(CF.face(12, .medium))
                    Spacer(minLength: 0)
                }
                .foregroundStyle(CF.machineText).frame(minHeight: 28, alignment: .leading).contentShape(Rectangle())
            }
            .buttonStyle(.plain).routed(.addOpen)
            .padding(.horizontal, MenuMetrics.horizontal).padding(.top, 6)
            if state.addOpen {
                VStack(spacing: 0) {
                    if candidates.isEmpty {
                        Text("Nothing to add.").font(CF.face(12)).foregroundStyle(CF.quiet).padding(.vertical, 6)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    ForEach(candidates, id: \.self) { id in
                        AddTileRow(title: name(id, plugins: plugins), plugin: TileID.isPlugin(id), control: .addTile(tab, id), state: state)
                    }
                }
                .padding(.horizontal, 6)
            }
            if let chart, let choice {
                MenuDivider(inset: true).padding(.vertical, 10)
                VStack(alignment: .leading, spacing: 10) {
                    if choice.metrics.count > 1 {
                        SettingRow("Chart") {
                            ForEach(choice.metrics, id: \.self) { metric in
                                Chip(metric.title, selected: choice.metric == metric, control: .chartMetric(tab, chart, metric), state: state)
                            }
                        }
                    }
                    SettingRow(choice.metrics.count > 1 ? "Range" : "Chart range") {
                        ForEach(PanelPrefs.rangeChoices, id: \.self) { days in
                            Chip("\(days) days", selected: choice.days == days, control: .chartRange(tab, chart, days), state: state)
                        }
                    }
                }
                .padding(.horizontal, MenuMetrics.horizontal - 8)
            }
            Spacer(minLength: 0).frame(height: 10)
        }
    }
}

struct TileSwitchRow: View {
    let title: String
    let on: Bool
    var grip = false
    let control: MenuControl
    @ObservedObject var state: MenuState

    var body: some View {
        HStack(spacing: 8) {
            if grip {
                Image(systemName: "line.3.horizontal").font(.system(size: 10, weight: .semibold)).foregroundStyle(CF.faint)
                    .frame(width: 14).accessibilityHidden(true)
            }
            Text(title).font(CF.face(13, .medium)).lineLimit(1)
            Spacer(minLength: 4)
            SwitchView(on: on).frame(width: 44, height: 30).contentShape(Rectangle()).routed(control)
                .accessibilityElement(children: .ignore).accessibilityLabel(title)
                .accessibilityValue(on ? "on" : "off").accessibilityAddTraits(.isButton)
        }
        .padding(.leading, 10).padding(.trailing, 4).frame(minHeight: 34)
        .background(RoundedRectangle(cornerRadius: 8).fill(CF.tile))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(CF.tileEdge, lineWidth: 0.5))
    }
}

struct AddTileRow: View {
    let title: String
    let plugin: Bool
    let control: MenuControl
    @ObservedObject var state: MenuState
    @State private var hovering = false

    var body: some View {
        Button { state.onControl(control) } label: {
            HStack(spacing: 8) {
                Image(systemName: "plus.circle").font(.system(size: 12)).foregroundStyle(CF.machineText).frame(width: 16)
                Text(title).font(CF.face(13)).lineLimit(1)
                if plugin { Text("Plugin").font(CF.face(11)).foregroundStyle(CF.quiet) }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10).frame(minHeight: 30)
            .background(RoundedRectangle(cornerRadius: 6).fill(hovering ? CF.hover : Color.clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain).routed(control).onHover { hovering = $0 }
        .accessibilityLabel("Add \(title)")
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
    static var labelWidth: CGFloat { 74 }
    let label: String
    @ViewBuilder let content: Content

    init(_ label: String, @ViewBuilder content: () -> Content) {
        self.label = label
        self.content = content()
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 0) {
            Text(label).font(CF.face(11.5, .medium)).foregroundStyle(CF.quiet).lineLimit(2)
                .frame(width: Self.labelWidth, alignment: .leading).padding(.leading, 8)
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
