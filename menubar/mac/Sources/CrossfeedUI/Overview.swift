// Crossfeed menu: the Overview tab. One compact row per provider, then the month's subscriptions and a
// quiet link to the console's usage page. Tapping a row opens that provider's tab.
import CrossfeedCore
import SwiftUI

struct OverviewView: View {
    let presenter: Presenter
    @ObservedObject var state: MenuState

    var body: some View {
        let order = state.layout.overviewIDs(state.orderedTabs.map(\.id))
        let rows = presenter.overviewRows(order: order)
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(rows) { row in OverviewRowView(row: row, state: state) }
            }
            .padding(.horizontal, 6).padding(.top, 8)
            MenuDivider(inset: true).padding(.top, 10).padding(.bottom, 6)
            SpendCard(model: presenter.spend).padding(.vertical, 4)
            LinkRow(title: "Usage and cost", control: .usage(nil), state: state).padding(.top, 4)
        }
        .padding(.bottom, 8)
    }
}

struct OverviewRowView: View {
    let row: OverviewRow
    @ObservedObject var state: MenuState
    @State private var hovering = false

    private var dot: Color? {
        switch row.flag {
        case .none: return nil
        case .minor: return CF.between
        case .severe, .usedUp: return CF.human
        }
    }

    private var flagWord: String? {
        switch row.flag {
        case .none: return nil
        case .minor: return "Degraded"
        case .severe: return "Incident"
        case .usedUp: return "Used up"
        }
    }

    var body: some View {
        Button { state.onControl(.openTab(row.id)) } label: {
            HStack(alignment: .top, spacing: 10) {
                Group {
                    if let logo = row.logo { ProviderMark(logo: logo, size: 18) } else { Color.clear }
                }
                .frame(width: 18, height: 18).padding(.top, 1)
                VStack(alignment: .leading, spacing: 7) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(row.title).font(CF.face(13.5, .medium)).lineLimit(1)
                        if let dot {
                            Circle().fill(dot).frame(width: 6, height: 6).alignmentGuide(.firstTextBaseline) { $0[VerticalAlignment.center] + 3 }
                                .accessibilityHidden(true)
                        }
                        Spacer(minLength: 4)
                        Measure(row.usedText, size: 12.5, weight: .medium).fixedSize()
                            .foregroundStyle(row.flag == .usedUp ? CF.humanText : CF.ink)
                        Measure(row.reset ?? "", size: 11.5).foregroundStyle(CF.quiet).lineLimit(1).fixedSize()
                            .frame(minWidth: 52, alignment: .trailing)
                    }
                    if let bar = row.bar {
                        GeometryReader { geo in
                            ZStack(alignment: .leading) {
                                Capsule().fill(CF.ink.opacity(0.14))
                                Capsule().fill(bar.hot ? CF.human : CF.machine)
                                    .frame(width: max(3, geo.size.width * min(1, bar.used / 100)))
                            }
                        }
                        .frame(height: 4)
                    } else {
                        Capsule().fill(CF.ink.opacity(0.08)).frame(height: 4)
                    }
                }
            }
            .padding(.horizontal, 10).padding(.vertical, 8)
            .background(RoundedRectangle(cornerRadius: 6).fill(hovering ? CF.ink.opacity(0.07) : Color.clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain).routed(.openTab(row.id)).onHover { hovering = $0 }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(row.title), \(row.window) \(row.usedText)" + (row.reset.map { ", resets \($0)" } ?? "") + (flagWord.map { ", \($0)" } ?? ""))
        .accessibilityAddTraits(.isButton)
    }
}
