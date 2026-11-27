// Crossfeed menu: the B+ tile parts. Every tile is a rounded white card on the sunk panel; the parts (limit row,
// resets, tokens, policy, credits, links, plugin rows) are the same wherever they appear.
import CrossfeedCore
import SwiftUI

/// The card: opaque tile colour (so a lifted tile covers what it passes over), a hairline edge, 8 pt corners.
struct TileChrome<Content: View>: View {
    var vertical: CGFloat = 10
    @ViewBuilder let content: Content

    var body: some View {
        content
            .padding(.horizontal, 12).padding(.vertical, vertical)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(RoundedRectangle(cornerRadius: 8).fill(CF.tile))
            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(CF.tileEdge, lineWidth: 0.5))
    }
}

/// Text in the figure style: tabular digits.
func figure(_ text: String, size: CGFloat = 13, weight: CF.Weight = .semibold) -> Text {
    Text(text).font(CF.face(size, weight)).monospacedDigit()
}

struct Triangle: Shape {
    func path(in rect: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: rect.midX, y: rect.minY))
        p.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
        p.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
        p.closeSubpath()
        return p
    }
}

/// The pace word with its mark: a terracotta triangle for deficit, an olive dot for surplus.
struct PaceWordView: View {
    let pace: PaceWord

    var body: some View {
        HStack(spacing: 5) {
            switch pace.kind {
            case .deficit: Triangle().fill(CF.human).frame(width: 7, height: 6)
            case .surplus: Circle().fill(CF.between).frame(width: 6, height: 6)
            case .onPace: Circle().fill(CF.machine).frame(width: 6, height: 6)
            }
            Text(pace.text).font(CF.face(12)).monospacedDigit()
                .foregroundStyle(pace.kind == .deficit ? CF.humanText : CF.quiet).lineLimit(1).fixedSize()
        }
        .accessibilityElement(children: .combine)
    }
}

/// A window: the figure where the eye lands, the window named top right, the bar, then the pace.
struct LimitRowView: View {
    let line: LimitLine

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                figure(line.figure).foregroundStyle(line.hot ? CF.humanText : CF.ink).fixedSize()
                Spacer(minLength: 0)
                (Text(line.window).font(CF.face(12, .medium)).foregroundColor(CF.ink)
                    + Text(" · " + line.resets).font(CF.face(12)).foregroundColor(CF.quiet))
                    .monospacedDigit().lineLimit(1).minimumScaleFactor(0.8).truncationMode(.tail)
            }
            .frame(minHeight: 17)
            PaceBar(model: line.bar, label: line.accessibility).padding(.top, 5).padding(.bottom, 4)
            if line.pace != nil || line.runsOut != nil {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if let pace = line.pace { PaceWordView(pace: pace) }
                    Spacer(minLength: 0)
                    if let runs = line.runsOut {
                        Text(runs).font(CF.face(12)).monospacedDigit().foregroundStyle(CF.humanText).lineLimit(1).fixedSize()
                    }
                }
                .frame(minHeight: 16)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

struct LimitsTile: View {
    let groups: [LimitGroup]
    let notMetered: [String]

    var body: some View {
        TileChrome {
            if !notMetered.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Not metered").font(CF.face(13, .medium))
                    Text(notMetered.joined(separator: " · ")).font(CF.face(12)).foregroundStyle(CF.quiet)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(groups.enumerated()), id: \.element.id) { index, group in
                        VStack(alignment: .leading, spacing: group.title != nil && group.lines.isEmpty ? 0 : 12) {
                            if index > 0 { Rectangle().fill(CF.rule).frame(height: 1).padding(.bottom, 0) }
                            if let title = group.title {
                                (Text(title).font(CF.face(12, .semibold)).foregroundColor(CF.quiet)
                                    + Text(group.note.map { " · " + $0 } ?? "").font(CF.face(12)).foregroundColor(CF.quiet))
                            }
                            ForEach(group.lines) { line in LimitRowView(line: line) }
                        }
                        .padding(.top, index > 0 ? 12 : 0)
                    }
                }
            }
        }
    }
}

// MARK: Resets

struct PrimaryButton: View {
    let title: String
    let control: MenuControl
    var primary = false
    var disabled = false
    @ObservedObject var state: MenuState
    @State private var hovering = false

    var body: some View {
        Button { state.onControl(control) } label: {
            Text(title).font(CF.face(12, primary ? .semibold : .medium)).lineLimit(1)
                .foregroundStyle(primary ? Color.white : CF.ink)
                .padding(.horizontal, primary ? 12 : 9).frame(height: primary ? 24 : 22)
                .background(RoundedRectangle(cornerRadius: 6).fill(primary ? CF.machine : (hovering ? CF.buttonHover : CF.button)))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain).routed(control).onHover { hovering = $0 }
        .disabled(disabled)
        .accessibilityLabel(title)
    }
}

struct ResetsTile: View {
    let rows: [ResetRow]
    @ObservedObject var state: MenuState

    var body: some View {
        TileChrome {
            VStack(alignment: .leading, spacing: 4) {
                Text("Resets").font(CF.face(12, .semibold)).foregroundStyle(CF.quiet)
                ForEach(rows) { row in
                    if let flow = state.resetFlow, flow.key == row.id {
                        switch flow.phase {
                        case .confirm: confirm(row)
                        case .running: line(row) { Text("Using…").font(CF.face(12)).foregroundStyle(CF.quiet) }
                        case .done(let left):
                            line(row) { Text("Used. Left: \(left)").font(CF.face(12)).monospacedDigit().foregroundStyle(CF.quiet) }
                        case .failed(let why):
                            line(row) { Text(why).font(CF.face(12)).foregroundStyle(CF.humanText).lineLimit(2).multilineTextAlignment(.trailing) }
                        }
                    } else {
                        line(row) {
                            if let until = row.until { Text("until " + until).font(CF.face(12)).monospacedDigit().foregroundStyle(CF.quiet).lineLimit(1) }
                            if row.canUse { PrimaryButton(title: "Use", control: .resetAsk(row.id), state: state) }
                        }
                    }
                }
            }
        }
    }

    private func line<Trailing: View>(_ row: ResetRow, @ViewBuilder trailing: () -> Trailing) -> some View {
        HStack(spacing: 6) {
            Text("🎟️").font(.system(size: 13)).frame(width: 16)
            Text(row.name).font(CF.face(13, .medium)).lineLimit(1).fixedSize()
            if row.next {
                Text("Next").font(CF.face(10, .semibold)).foregroundStyle(CF.machineText).padding(.horizontal, 4).frame(height: 16)
                    .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(CF.machineText.opacity(0.45), lineWidth: 1))
            }
            Spacer(minLength: 4)
            trailing()
        }
        .frame(minHeight: 24)
        .accessibilityElement(children: .contain)
    }

    private func confirm(_ row: ResetRow) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            (Text(row.ask).font(CF.face(13, .medium)) + Text("\n") + Text("This can't be undone.").font(CF.face(12)).foregroundColor(CF.quiet))
                .fixedSize(horizontal: false, vertical: true)
            if let effect = row.effect {
                Text(effect).font(CF.face(12)).monospacedDigit().foregroundStyle(CF.quiet).fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 6) {
                Spacer(minLength: 0)
                PrimaryButton(title: "Cancel", control: .resetCancel, state: state)
                PrimaryButton(title: "Use", control: .resetUse(row.id), primary: true, state: state)
            }
            .padding(.top, 6)
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(CF.surface))
        .padding(.horizontal, -4)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(row.ask + " This can't be undone.")
    }
}

// MARK: Tokens, credits

struct TokensTile: View {
    let tokens: TokensModel

    private var label: some View {
        (figure(tokens.today).foregroundColor(CF.ink) + Text(" tokens today").font(CF.face(12)).foregroundColor(CF.quiet))
            .lineLimit(1).fixedSize()
    }

    var body: some View {
        TileChrome {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) { label; Spacer(minLength: 0); if !tokens.days.isEmpty { Sparkline(days: tokens.days) } }
                VStack(alignment: .leading, spacing: 6) { label; if !tokens.days.isEmpty { Sparkline(days: tokens.days) } }
            }
        }
        .accessibilityElement(children: .combine)
    }
}

struct CreditsTile: View {
    let credits: CreditsModel

    private var amount: some View {
        HStack(spacing: 6) {
            Text("🪙").font(.system(size: 13)).frame(width: 16)
            figure(credits.amount, weight: .medium).lineLimit(1).fixedSize()
        }
    }

    var body: some View {
        TileChrome {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) {
                    amount; Spacer(minLength: 0)
                    if let until = credits.until { Text(until).font(CF.face(12)).monospacedDigit().foregroundStyle(CF.quiet).lineLimit(1).fixedSize() }
                }
                VStack(alignment: .leading, spacing: 3) {
                    amount
                    if let until = credits.until { Text(until).font(CF.face(12)).monospacedDigit().foregroundStyle(CF.quiet).padding(.leading, 22) }
                }
            }
        }
    }
}

// MARK: Policy

struct PolicyTile: View {
    let policy: PolicyModel
    @ObservedObject var state: MenuState
    let now: Date
    @State private var hovering = false

    private var level: String {
        state.edits.selection(for: policy.id, stored: policy.level)?.label ?? "Mixed"
    }

    var body: some View {
        TileChrome {
            VStack(alignment: .leading, spacing: 6) {
                Text("Spend policy").font(CF.face(12)).foregroundStyle(CF.quiet)
                Button { state.onControl(.policyOpen) } label: {
                    HStack {
                        Text(level).font(CF.face(12, .semibold))
                        Spacer(minLength: 0)
                        Image(systemName: state.policyOpen ? "chevron.up" : "chevron.up.chevron.down")
                            .font(.system(size: 9, weight: .semibold)).foregroundStyle(CF.quiet)
                    }
                    .padding(.leading, 10).padding(.trailing, 8).frame(height: 24)
                    .background(RoundedRectangle(cornerRadius: 6).fill(hovering ? CF.buttonHover : CF.button))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain).routed(.policyOpen).onHover { hovering = $0 }
                .accessibilityLabel("Spend policy").accessibilityValue(level)
                if state.policyOpen { PolicyControl(policy: policy, state: state, now: now, showsTitle: false).padding(.top, 2) }
            }
        }
    }
}

// MARK: Links

/// "Usage and cost ↗", "Status ↗", "Console ↗": the places the menu sends you, last on every tab.
struct LinksTile: View {
    let links: [LinkItem]
    @ObservedObject var state: MenuState

    private func control(_ link: LinkItem) -> MenuControl {
        switch link.kind {
        case .usage(let pool): return .usage(pool)
        case .status(let url): return .openURL(url)
        case .console: return .console
        }
    }

    var body: some View {
        TileChrome(vertical: 9) {
            HStack(spacing: 8) {
                ForEach(Array(links.enumerated()), id: \.element.id) { index, link in
                    if index > 0 { Spacer(minLength: 0) }
                    LinkLabel(title: link.title, control: control(link), state: state)
                }
            }
        }
    }
}

struct LinkLabel: View {
    let title: String
    let control: MenuControl
    @ObservedObject var state: MenuState
    @State private var hovering = false

    var body: some View {
        Button { state.onControl(control) } label: {
            HStack(spacing: 4) {
                Text(title).font(CF.face(12, .medium))
                Image(systemName: "arrow.up.right").font(.system(size: 9, weight: .semibold))
            }
            .foregroundStyle(CF.machineText).opacity(hovering ? 0.75 : 1)
            .frame(minHeight: 20).contentShape(Rectangle())
        }
        .buttonStyle(.plain).routed(control).onHover { hovering = $0 }
        .accessibilityAddTraits(.isLink)
    }
}

// MARK: Plugins

/// A plugin's tile document, drawn with the same parts as the built-ins.
struct PluginTile: View {
    let entry: PluginEntry
    let show: PanelPrefs.Show
    @ObservedObject var state: MenuState

    var body: some View {
        TileChrome {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 6) {
                    Text(entry.doc.title).font(CF.face(12, .semibold)).foregroundStyle(CF.quiet).lineLimit(1)
                    if entry.doc.status != .ok {
                        Circle().fill(entry.doc.status == .error ? CF.human : CF.between).frame(width: 6, height: 6)
                    }
                    Spacer(minLength: 0)
                }
                ForEach(Array(entry.doc.rows.enumerated()), id: \.offset) { _, row in rowView(row) }
            }
        }
    }

    @ViewBuilder private func rowView(_ row: PluginRow) -> some View {
        switch row.kind {
        case .bar(let used, let pace, let window, let resetsAt):
            let line = pluginLine(row.label, used: used, pace: pace, window: window, resetsAt: resetsAt)
            LimitRowView(line: line)
        case .value(let value, let tone):
            HStack(alignment: .firstTextBaseline) {
                Text(row.label).font(CF.face(13)).lineLimit(1)
                Spacer(minLength: 4)
                figure(value).foregroundStyle(tone == .error ? CF.humanText : (tone == .warn ? CF.betweenText : CF.ink)).lineLimit(1)
            }
            .accessibilityElement(children: .combine)
        case .sparkline(let values, let today):
            HStack(alignment: .center) {
                (figure(today ?? "", size: 13).foregroundColor(CF.ink) + Text((today == nil ? "" : " ") + row.label).font(CF.face(12)).foregroundColor(CF.quiet))
                    .lineLimit(1)
                Spacer(minLength: 4)
                Sparkline(days: values.map { Int64($0) })
            }
            .accessibilityElement(children: .combine)
        case .text:
            Text(row.label).font(CF.face(12)).foregroundStyle(CF.quiet).fixedSize(horizontal: false, vertical: true)
        case .link(let url):
            LinkLabel(title: row.label, control: .openURL(url), state: state)
        }
    }

    private func pluginLine(_ label: String, used: Double, pace: Double?, window: String?, resetsAt: String?) -> LimitLine {
        let full = used >= 99.5
        let delta = pace.map { used - $0 }
        let when = resetsAt.flatMap { Presenter.controlDate($0) }.flatMap { date in state.presenter?.whenText(date) }
        let word: PaceWord? = full ? nil : delta.map {
            abs($0) < 1 ? PaceWord(text: "on pace", kind: .onPace)
                : PaceWord(text: "\(Int(abs($0).rounded()))% \($0 > 0 ? "deficit" : "surplus")", kind: $0 > 0 ? .deficit : .surplus)
        }
        let figureText = full ? "Used up" : (show == .used ? "\(Int(used.rounded()))% used" : "\(Int((100 - used).rounded()))% left")
        let name = [label, window].compactMap { $0 }.joined(separator: " ")
        return LimitLine(id: label, figure: figureText, hot: full || used >= 90, window: name,
                         resets: when.map { "resets " + $0 } ?? "", bar: PaceBarModel.make(used: used, elapsed: full ? nil : pace, show: show, full: full),
                         pace: word, runsOut: nil, used: used, full: full)
    }
}

// MARK: Overview

struct ProviderCardTile: View {
    let card: ProviderCard
    @ObservedObject var state: MenuState

    var body: some View {
        Button { state.onControl(.openTab(card.id)) } label: {
            TileChrome(vertical: 8) {
                VStack(alignment: .leading, spacing: 0) {
                    HStack(alignment: .center, spacing: 7) {
                        if let logo = card.logo { ProviderMark(logo: logo, size: 16) }
                        Text(card.title).font(CF.face(13, .semibold)).lineLimit(1)
                        if card.tickets > 0 {
                            HStack(spacing: 3) {
                                Text("🎟️").font(.system(size: 12))
                                Text("\(card.tickets)").font(CF.face(12, .medium)).monospacedDigit().foregroundStyle(CF.betweenText)
                            }
                            .accessibilityLabel("\(card.tickets) limit resets held")
                        }
                        Spacer(minLength: 4)
                        figure(card.line?.figure ?? "Not read").foregroundStyle(card.line?.hot == true ? CF.humanText : CF.ink).fixedSize()
                    }
                    .frame(minHeight: 17)
                    if let line = card.line {
                        PaceBar(model: line.bar, height: 5, label: line.accessibility).padding(.top, 5).padding(.bottom, 4)
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            (Text(line.window).font(CF.face(12, .medium)).foregroundColor(CF.ink)
                                + Text(" · " + line.resets).font(CF.face(12)).foregroundColor(CF.quiet))
                                .monospacedDigit().lineLimit(1).minimumScaleFactor(0.8).truncationMode(.tail)
                            Spacer(minLength: 0)
                            if let pace = line.pace, !line.full { PaceWordView(pace: pace) }
                        }
                        .frame(minHeight: 16)
                    } else {
                        Capsule().fill(CF.track).frame(height: 5).padding(.top, 5)
                    }
                }
            }
        }
        .buttonStyle(.plain).routed(.openTab(card.id))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(card.title), " + (card.line?.accessibility ?? "not read"))
        .accessibilityAddTraits(.isButton)
    }
}

struct SubscriptionsTile: View {
    let model: SubscriptionsModel

    var body: some View {
        TileChrome {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(model.rows) { row in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(row.name).font(CF.face(13)).lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                        Text(row.share ?? "").font(CF.face(12)).foregroundStyle(CF.quiet).frame(width: 12)
                        Text(row.full ?? "").font(CF.face(12)).monospacedDigit().foregroundStyle(CF.quiet).frame(width: 44, alignment: .trailing)
                        Text(row.yours).font(CF.face(row.priced ? 13 : 12)).monospacedDigit()
                            .foregroundStyle(row.priced ? CF.ink : CF.quiet).lineLimit(1).frame(minWidth: 52, alignment: .trailing)
                    }
                    .frame(minHeight: 17)
                    .accessibilityElement(children: .combine)
                }
                if let total = model.total {
                    Rectangle().fill(CF.rule).frame(height: 1).padding(.top, 3)
                    HStack(alignment: .firstTextBaseline) {
                        Text("You pay a month").font(CF.face(13, .semibold))
                        Spacer(minLength: 4)
                        Text(total).font(CF.face(13, .semibold)).monospacedDigit()
                    }
                    .padding(.top, 3).accessibilityElement(children: .combine)
                }
            }
        }
    }
}
