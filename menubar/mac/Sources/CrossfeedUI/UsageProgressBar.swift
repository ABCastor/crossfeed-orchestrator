// Ported from CodexBar's UsageProgressBar.swift (MIT, Copyright (c) 2026 Peter Steinberger).
// The track, the fill and the punched-out pace stripe are drawn in one Canvas, as upstream does, so no
// SwiftUI compositing modifier is involved. The quota-warning and work-day markers are not ported:
// the glance JSON carries neither. Colours follow Crossfeed: the stripe is olive when the window is
// ahead of an even burn (surplus) and ink when it is behind (deficit), where upstream uses green and red.
// Crossfeed also shades the gap the stripe marks, so pace reads without the numbers: in deficit the
// part of the fill beyond the stripe is terracotta (spent ahead of time); in surplus the stretch
// between the fill and the stripe is olive (allowance that will expire unspent), as in the console.
import CrossfeedCore
import SwiftUI

public typealias PaceKindUI = PaceKind

struct UsageProgressBar: View {
    let percent: Double
    let tint: Color
    let pacePercent: Double?
    let paceOnTop: Bool
    let paceColor: Color
    let overColor: Color
    let unspentColor: Color
    let accessibilityLabel: String
    @Environment(\.displayScale) private var displayScale

    private static let paceStripeWidth: CGFloat = 2

    init(percent: Double, tint: Color, accessibilityLabel: String, pacePercent: Double? = nil,
         paceOnTop: Bool = true, paceColor: Color = CF.between, overColor: Color = CF.human,
         unspentColor: Color = CF.between.opacity(0.42)) {
        self.overColor = overColor
        self.unspentColor = unspentColor
        self.percent = percent
        self.tint = tint
        self.accessibilityLabel = accessibilityLabel
        self.pacePercent = pacePercent
        self.paceOnTop = paceOnTop
        self.paceColor = paceColor
    }

    private var clamped: Double { min(100, max(0, percent)) }

    var body: some View {
        Canvas { context, size in
            let scale = max(displayScale, 1)
            let fillPercent = Self.renderedFillPercent(clamped)
            let fillWidth = size.width * fillPercent / 100
            let paceWidth = size.width * Self.clampedPercent(pacePercent) / 100
            let tipWidth = max(25, size.height * 6.5)
            let stripeSpan = Self.paceStripeWidth * 3
            let tipOffset = paceWidth - tipWidth + stripeSpan / 2 + 1 / scale
            let showTip = pacePercent != nil

            let cornerRadius = size.height / 2
            let cornerSize = CGSize(width: cornerRadius, height: cornerRadius)
            let rect = CGRect(origin: .zero, size: size)
            context.clip(to: Path(rect))

            context.fill(Path { $0.addRoundedRect(in: rect, cornerSize: cornerSize) }, with: .color(CF.ink.opacity(0.14)))
            if showTip, paceOnTop, paceWidth > fillWidth {
                // Surplus: the stretch the window has already earned but nobody has used.
                let gapRect = CGRect(x: 0, y: 0, width: min(paceWidth, size.width), height: size.height)
                context.fill(Path { $0.addRoundedRect(in: gapRect, cornerSize: cornerSize) }, with: .color(unspentColor))
            }
            if fillWidth > 0 {
                let fillRect = CGRect(x: 0, y: 0, width: min(fillWidth, size.width), height: size.height)
                let fillPath = Path { $0.addRoundedRect(in: fillRect, cornerSize: cornerSize) }
                if showTip, !paceOnTop, paceWidth < fillWidth {
                    // Deficit: teal up to an even burn, terracotta for what was spent ahead of it.
                    context.fill(fillPath, with: .color(overColor))
                    context.drawLayer { layer in
                        layer.clip(to: Path(CGRect(x: 0, y: 0, width: paceWidth, height: size.height)))
                        layer.fill(fillPath, with: .color(tint))
                    }
                } else {
                    context.fill(fillPath, with: .color(tint))
                }
            }
            if showTip {
                let stripes = Self.paceStripePaths(size: CGSize(width: tipWidth, height: size.height), scale: scale)
                let shift = CGAffineTransform(translationX: tipOffset, y: 0)
                context.blendMode = .destinationOut
                context.fill(stripes.punched.applying(shift), with: .color(.white.opacity(0.9)))
                context.blendMode = .normal
                context.fill(stripes.center.applying(shift), with: .color(paceColor))
            }
        }
        .frame(height: 6)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityValue("\(Int(clamped.rounded())) percent")
    }

    /// Aligns edge rendering with the rounded percent label: sub-0.5% is empty and 99.5%+ is full.
    static func renderedFillPercent(_ percent: Double) -> Double {
        let clamped = clampedPercent(percent)
        let display = Int(clamped.rounded())
        if display <= 0 { return 0 }
        if display >= 100 { return 100 }
        return clamped
    }

    private static func paceStripePaths(size: CGSize, scale: CGFloat) -> (punched: Path, center: Path) {
        let rect = CGRect(origin: .zero, size: size)
        let extend = size.height * 2
        let align: (CGFloat) -> CGFloat = { ($0 * scale).rounded() / scale }
        let stripeWidth = paceStripeWidth
        let punchWidth = stripeWidth * 3
        let anchorX = align(rect.maxX - 1 / scale)
        let minY = align(-extend), maxY = align(size.height + extend)
        guard (anchorX - punchWidth) - rect.minX >= 0 else { return (Path(), Path()) }
        let punchLeft = align(anchorX) - punchWidth
        var punched = Path(), center = Path()
        punched.addRect(CGRect(x: punchLeft, y: minY, width: punchWidth, height: maxY - minY))
        let centerLeft = align(punchLeft + (punchWidth - stripeWidth) / 2)
        center.addRect(CGRect(x: centerLeft, y: minY, width: stripeWidth, height: maxY - minY))
        return (punched, center)
    }

    private static func clampedPercent(_ value: Double?) -> Double {
        guard let value else { return 0 }
        return min(100, max(0, value))
    }
}
