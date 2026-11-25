import CrossfeedCore
import SwiftUI

/// Five click stops on the console's rail: completed dots, paper-ringed knob, hollow future stops.
struct PolicyControl: View {
    let policy: PolicyModel
    @ObservedObject var state: MenuState
    let now: Date
    @State private var hovered: SpendLevel?

    private let levels = SpendLevel.allCases
    private var selected: SpendLevel? { state.edits.selection(for: policy.id, stored: policy.level) }
    private var index: Int? { selected.flatMap { levels.firstIndex(of: $0) } }
    private var saving: Bool { state.edits.pending?.pool == policy.id }
    private var railTint: Color {
        switch policy.attribution?.actor {
        case .operator_: return CF.human
        case .agent: return CF.machine
        case nil: return CF.quiet
        }
    }
    private var textTint: Color {
        switch policy.attribution?.actor {
        case .operator_: return CF.humanText
        case .agent: return CF.machineText
        case nil: return CF.quiet
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("Spend policy").font(CF.face(11.5, .semibold))
                if selected == nil {
                    Spacer()
                    Text("Mixed").font(CF.face(11.5, .medium)).foregroundStyle(CF.quiet)
                } else if selected == .forced {
                    Spacer()
                    Text("Ignores quota estimates").font(CF.face(10.5)).foregroundStyle(CF.quiet)
                }
            }
            HStack(spacing: 0) {
                ForEach(Array(levels.enumerated()), id: \.element) { stop, level in stopButton(level, at: stop) }
            }
            .background(alignment: .top) {
                GeometryReader { geo in
                    let inset = geo.size.width / CGFloat(2 * levels.count)
                    let width = geo.size.width - 2 * inset
                    ZStack(alignment: .leading) {
                        Capsule().fill(CF.rule)
                        if let index {
                            Capsule().fill(railTint).frame(width: width * CGFloat(index) / CGFloat(levels.count - 1))
                        }
                    }
                    .frame(width: width, height: 2)
                    .offset(x: inset, y: 11)
                }
                .allowsHitTesting(false)
                .accessibilityHidden(true)
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Spend policy")
            .accessibilityValue(selected?.label ?? "Mixed")
            Text(saving ? "Saving…" : state.edits.notices[policy.id]
                 ?? policy.attribution?.line(now: now) ?? "No recorded change")
                .font(CF.face(11)).lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .center)
                .foregroundStyle(state.edits.notices[policy.id] == nil ? textTint : CF.humanText)
        }
        .padding(.top, 3)
    }

    private func stopButton(_ level: SpendLevel, at stop: Int) -> some View {
        let change = PolicyChange(pool: policy.id, level: level)
        let active = level == selected
        let filled = index.map { stop < $0 } ?? false
        return Button { state.onPolicyChange(change) } label: {
            VStack(spacing: 3) {
                dot(level, active: active, filled: filled).frame(height: 24)
                Text(level.label)
                    .font(CF.face(10.5, active ? .semibold : .medium))
                    .foregroundStyle(active || hovered == level ? CF.ink : CF.quiet)
                    .multilineTextAlignment(.center).fixedSize(horizontal: true, vertical: true)
            }
            .frame(maxWidth: .infinity, minHeight: 42, alignment: .top)
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
        .help(level == .forced ? "Max ignores quota estimates" : level.label)
        .accessibilityLabel(level.label)
        .accessibilityAddTraits(active ? .isSelected : [])
    }

    private func dot(_ level: SpendLevel, active: Bool, filled: Bool) -> some View {
        let off = active && level == .off
        let border = active || filled ? railTint : (hovered == level ? CF.ink : CF.ruleStrong)
        return ZStack {
            if active { Circle().fill(CF.paper).frame(width: 28, height: 28) }
            Circle().fill(!off && (active || filled) ? railTint : CF.paper)
                .overlay { Circle().strokeBorder(border, lineWidth: 1.5) }
                .frame(width: active ? 20 : 10, height: active ? 20 : 10)
                .scaleEffect(!active && hovered == level ? 1.3 : 1)
            if off { Capsule().fill(railTint).frame(width: 1.5, height: 18).rotationEffect(.degrees(45)) }
        }
        .accessibilityHidden(true)
    }
}

struct PolicyFrames: PreferenceKey {
    static var defaultValue: [PolicyChange: CGRect] = [:]
    static func reduce(value: inout [PolicyChange: CGRect], nextValue: () -> [PolicyChange: CGRect]) {
        value.merge(nextValue()) { $1 }
    }
}
