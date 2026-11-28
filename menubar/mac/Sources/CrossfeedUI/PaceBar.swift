// Crossfeed menu: the pace bar and the tokens sparkline, drawn in one Canvas each (B+ Tiles tokens).
//
// The bar: a rounded track, then segments flush against each other with no gaps. Fill is teal. Past the pace tick
// the segment is terracotta stripes (deficit); short of it, from the fill's end exactly to the tick, it is green
// stripes (surplus): the same diagonal stripes (135 degrees, 1.5 pt of stripe in every 4 pt), the full bar height.
// The tick is a 2 pt marker that rises 3 pt above and below the bar, ringed in the tile's colour.
import CrossfeedCore
import SwiftUI

struct PaceBar: View {
    let model: PaceBarModel
    var height: CGFloat = 6
    var ring: Color = CF.tile
    let label: String

    var body: some View {
        let overhang: CGFloat = 3
        Canvas { context, size in
            let w = size.width
            let bar = CGRect(x: 0, y: overhang, width: w, height: height)
            var inner = context
            inner.clip(to: Path(roundedRect: bar, cornerRadius: height / 2))
            inner.fill(Path(bar), with: .color(CF.track))
            for segment in model.segments {
                let x0 = w * min(100, max(0, segment.from)) / 100, x1 = w * min(100, max(0, segment.to)) / 100
                guard x1 > x0 else { continue }
                let rect = CGRect(x: x0, y: bar.minY, width: x1 - x0, height: height)
                switch segment.kind {
                case .fill:
                    inner.fill(Path(rect), with: .color(CF.machine))
                case .deficit, .surplus:
                    inner.fill(Path(rect), with: .color(segment.kind == .deficit ? CF.human : CF.spare))
                    var stripes = inner
                    stripes.clip(to: Path(rect))
                    stripes.fill(Self.hatch(in: rect), with: .color(CF.hatch))
                }
            }
            if let tick = model.tick {
                let x = w * min(100, max(0, tick)) / 100
                let marker = CGRect(x: x - 1, y: 0, width: 2, height: height + overhang * 2)
                context.fill(Path(roundedRect: marker.insetBy(dx: -1, dy: -1), cornerRadius: 2), with: .color(ring))
                context.fill(Path(roundedRect: marker, cornerRadius: 1), with: .color(CF.marker))
            }
        }
        .frame(height: height + overhang * 2)
        .padding(.vertical, -overhang)
        .accessibilityElement()
        .accessibilityLabel(label)
    }

    /// repeating-linear-gradient(135deg, stripe 0 1.5px, clear 1.5px 4px), from the segment's top left.
    static func hatch(in rect: CGRect) -> Path {
        var path = Path()
        let step = 4 * 2.0.squareRoot(), thick = 1.5 * 2.0.squareRoot()
        var u: CGFloat = 0
        while u < rect.width + rect.height {
            path.move(to: CGPoint(x: rect.minX + u, y: rect.minY))
            path.addLine(to: CGPoint(x: rect.minX + u + thick, y: rect.minY))
            path.addLine(to: CGPoint(x: rect.minX + u + thick - rect.height, y: rect.maxY))
            path.addLine(to: CGPoint(x: rect.minX + u - rect.height, y: rect.maxY))
            path.closeSubpath()
            u += step
        }
        return path
    }
}

/// Tokens per day, oldest first; today's bar is teal, empty days are a hairline.
struct Sparkline: View {
    let days: [Int64]
    /// Nil fills the width it is given.
    var width: CGFloat? = 52
    var height: CGFloat = 16
    var label: String?

    var body: some View {
        Canvas { context, size in
            let n = days.count
            guard n > 0 else { return }
            let gap: CGFloat = 1.5
            let bw = (size.width - gap * CGFloat(n - 1)) / CGFloat(n)
            let top = CGFloat(max(days.max() ?? 1, 1))
            for (i, v) in days.enumerated() {
                let bh = v > 0 ? max(2, (CGFloat(v) / top * size.height * 10).rounded() / 10) : 1
                let rect = CGRect(x: CGFloat(i) * (bw + gap), y: size.height - bh, width: bw, height: bh)
                let color: Color = i == n - 1 ? CF.machineText : (v > 0 ? CF.spark : CF.spark.opacity(0.55))
                context.fill(Path(roundedRect: rect, cornerRadius: min(1, bw / 2)), with: .color(color))
            }
        }
        .frame(width: width, height: height)
        .frame(maxWidth: width == nil ? .infinity : nil)
        .accessibilityLabel(label ?? "Per day, last \(days.count) days")
    }
}
