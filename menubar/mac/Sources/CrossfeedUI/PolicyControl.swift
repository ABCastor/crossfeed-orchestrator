import CrossfeedCore
import SwiftUI

struct PolicyControl: View {
    let section: SectionModel
    @ObservedObject var state: MenuState
    let now: Date
    @State private var hovered: SpendLevel?

    private var selected: SpendLevel? { state.edits.selection(for: section.id, stored: section.level) }
    private var saving: Bool { state.edits.pending?.pool == section.id }
    private var tint: Color {
        switch section.attribution?.actor {
        case .operator_: return CF.humanText
        case .agent: return CF.machineText
        case nil: return CF.quiet
        }
    }

    private var policyName: String {
        switch section.id {
        case "antigravity-gemini": return "Gemini spend policy (both windows)"
        case "antigravity-3p": return "Claude/GPT weekly spend policy"
        case "antigravity-argon": return "Argon weekly spend policy"
        default: return section.title.map { "\($0) spend policy" } ?? "Spend policy"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 0) {
                ForEach(SpendLevel.allCases, id: \.self) { level in
                    segment(level)
                }
            }
            .padding(2)
            .background(RoundedRectangle(cornerRadius: 6).fill(CF.surface))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(CF.rule, lineWidth: 1))
            .accessibilityElement(children: .contain)
            .accessibilityLabel(policyName)
            .help(policyName)
            Text(saving ? "Saving…" : state.edits.notices[section.id]
                 ?? section.attribution?.line(now: now) ?? "No recorded change")
                .font(CF.face(11)).lineLimit(1)
                .foregroundStyle(state.edits.notices[section.id] == nil ? tint : CF.humanText)
        }
        .padding(.top, 2)
    }

    private func segment(_ level: SpendLevel) -> some View {
        let change = PolicyChange(pool: section.id, level: level)
        let active = level == selected
        return Button { state.onPolicyChange(change) } label: {
            Text(level.label).font(CF.face(11, active ? .semibold : .regular))
                .lineLimit(1).fixedSize(horizontal: true, vertical: false)
                .padding(.horizontal, 4)
                .frame(maxWidth: .infinity, minHeight: 23)
                .foregroundStyle(active ? tint : CF.ink)
                .background(RoundedRectangle(cornerRadius: 4)
                    .fill(active ? tint.opacity(0.15) : hovered == level ? CF.ink.opacity(0.07) : .clear))
                .contentShape(Rectangle())
                .background(GeometryReader { geo in
                    Color.clear.preference(key: PolicyFrames.self,
                                           value: [change: geo.frame(in: .named(MenuMetrics.space))])
                })
        }
        .buttonStyle(.plain)
        .onHover { inside in
            if inside { hovered = level } else if hovered == level { hovered = nil }
        }
        .disabled(!state.canEditPolicy || state.edits.pending != nil)
        .accessibilityAddTraits(active ? .isSelected : [])
    }
}

struct PolicyFrames: PreferenceKey {
    static var defaultValue: [PolicyChange: CGRect] = [:]
    static func reduce(value: inout [PolicyChange: CGRect], nextValue: () -> [PolicyChange: CGRect]) {
        value.merge(nextValue()) { $1 }
    }
}
