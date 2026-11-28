// Crossfeed menu: the tiles of an API tab (Spend, Balance, Models) and the Status tiles. Same parts as the limit tiles:
// the figure where the eye lands, the pace bar, quiet words under it.
import CrossfeedCore
import SwiftUI

struct SpendTile: View {
    let model: ApiSpendModel

    private func stat(_ label: String, _ value: String?, bold: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label).font(CF.face(11)).foregroundStyle(CF.quiet)
            Text(value ?? "–").font(CF.face(13, bold ? .semibold : .medium)).monospacedDigit().foregroundStyle(value == nil ? CF.quiet : CF.ink)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    var body: some View {
        TileChrome {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    (figure(model.figure).foregroundColor(model.hot ? CF.humanText : CF.ink)
                        + Text(" " + model.caption).font(CF.face(12)).foregroundColor(CF.quiet)).lineLimit(1).fixedSize()
                    Spacer(minLength: 0)
                    if let detail = model.detail {
                        Text(detail).font(CF.face(12)).monospacedDigit().foregroundStyle(CF.quiet).lineLimit(1).minimumScaleFactor(0.8)
                    }
                }
                .frame(minHeight: 17)
                if let bar = model.bar {
                    PaceBar(model: bar, label: model.detail ?? "Spend").padding(.top, 5).padding(.bottom, 4)
                    if model.pace != nil || model.runsOut != nil {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            if let pace = model.pace { PaceWordView(pace: pace) }
                            Spacer(minLength: 0)
                            if let runs = model.runsOut {
                                Text(runs).font(CF.face(12)).monospacedDigit().foregroundStyle(CF.humanText).lineLimit(1).fixedSize()
                            }
                        }
                        .frame(minHeight: 16)
                    }
                }
                if model.today != nil || model.week != nil || model.month != nil {
                    HStack(alignment: .top, spacing: 8) {
                        stat("Today", model.today)
                        stat("Week", model.week)
                        stat("Month", model.month, bold: true)
                    }
                    .padding(.top, model.bar == nil ? 8 : 6)
                }
                if let source = model.source {
                    Text(source).font(CF.face(11)).foregroundStyle(CF.faint).padding(.top, 6)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }
}

struct BalanceTile: View {
    let model: ApiBalanceModel

    var body: some View {
        TileChrome {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(model.rows) { row in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(row.label).font(CF.face(12)).foregroundStyle(CF.quiet)
                        figure(row.figure).foregroundStyle(row.hot ? CF.humanText : CF.ink).lineLimit(1)
                        if let bar = row.bar { PaceBar(model: bar, height: 4, label: row.label).padding(.vertical, 2) }
                        Text(row.detail).font(CF.face(12)).monospacedDigit().foregroundStyle(CF.quiet).lineLimit(1).minimumScaleFactor(0.8)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
    }
}

struct ModelsTile: View {
    let rows: [ApiModelRow]

    var body: some View {
        TileChrome {
            VStack(alignment: .leading, spacing: 8) {
                Text("Models").font(CF.face(12, .semibold)).foregroundStyle(CF.quiet)
                ForEach(rows) { row in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text(row.name).font(CF.face(13, .medium)).lineLimit(1).truncationMode(.middle)
                            if let vendor = row.vendor { Text(vendor).font(CF.face(11)).foregroundStyle(CF.faint).lineLimit(1) }
                            Spacer(minLength: 4)
                            figure(row.usd, size: 13, weight: .medium).fixedSize()
                        }
                        HStack(spacing: 8) {
                            GeometryReader { geo in
                                ZStack(alignment: .leading) {
                                    Capsule().fill(CF.track)
                                    Capsule().fill(CF.machine).frame(width: max(2, geo.size.width * row.share))
                                }
                            }
                            .frame(height: 3)
                            if let requests = row.requests {
                                Text(requests).font(CF.face(11)).monospacedDigit().foregroundStyle(CF.quiet).lineLimit(1).fixedSize()
                            }
                        }
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
    }
}

private func statusColor(_ level: StatusModel.Level) -> Color {
    switch level {
    case .clear: return CF.machine
    case .notice: return CF.human
    case .severe: return CF.human
    case .unknown: return CF.faint
    }
}

struct StatusTile: View {
    let model: StatusModel

    var body: some View {
        TileChrome(vertical: 9) {
            HStack(alignment: .top, spacing: 8) {
                Circle().fill(statusColor(model.level)).frame(width: 7, height: 7).padding(.top, 5)
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.word).font(CF.face(13, .semibold)).foregroundStyle(model.level == .severe ? CF.humanText : CF.ink)
                    if let text = model.text {
                        Text(text).font(CF.face(12)).foregroundStyle(CF.quiet).fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 4)
                if model.url != nil {
                    Image(systemName: "arrow.up.right").font(.system(size: 9, weight: .semibold)).foregroundStyle(CF.machineText).padding(.top, 4)
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Status, " + model.word + (model.text.map { ", " + $0 } ?? ""))
    }
}

/// Overview: the incidents the providers report now, one row each; a row opens that provider's status page, else its tab.
struct OverviewStatusTile: View {
    let rows: [IncidentRow]
    @ObservedObject var state: MenuState

    var body: some View {
        TileChrome(vertical: 9) {
            VStack(alignment: .leading, spacing: 8) {
                if rows.isEmpty {
                    HStack(spacing: 8) {
                        Circle().fill(CF.machine).frame(width: 7, height: 7)
                        Text("No incidents").font(CF.face(13, .semibold))
                    }
                }
                ForEach(rows) { row in
                    HStack(alignment: .top, spacing: 8) {
                        Circle().fill(CF.human).frame(width: 7, height: 7).padding(.top, 5)
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 6) {
                                if let logo = row.logo { ProviderMark(logo: logo, size: 13) }
                                Text(row.title).font(CF.face(13, .semibold)).lineLimit(1)
                                Text(row.word).font(CF.face(12, .medium)).foregroundStyle(row.severe ? CF.humanText : CF.quiet).lineLimit(1)
                            }
                            Text(row.text).font(CF.face(12)).foregroundStyle(CF.quiet).fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 0)
                    }
                    .contentShape(Rectangle())
                    .routed(.at("status:" + row.id, row.url.map(LinkTarget.url) ?? .tab(row.id)))
                    .accessibilityElement(children: .combine)
                    .accessibilityAddTraits(.isButton)
                }
            }
        }
    }
}
