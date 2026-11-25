// Crossfeed menu bar icon: the mark's two tanks, filled to how much the two most pressed pools have used,
// joined by the pipe. Drawn per appearance so it follows the menu bar's light or dark ink; the left
// tank turns terracotta when that pool is in deficit. Shape idea after CodexBar's two-bar icon
// (IconRenderer.swift, MIT, Copyright (c) 2026 Peter Steinberger); the tanks are Crossfeed's mark.
import AppKit
import CrossfeedCore

public enum MenuBarIcon {
    public static let size = NSSize(width: 20, height: 16)

    public static func image(for summary: BarSummary) -> NSImage {
        let image = NSImage(size: size, flipped: false) { rect in
            let ink = NSColor.labelColor
            let tank = { (x: CGFloat) in NSRect(x: x, y: 1.5, width: 6.5, height: 13) }
            let left = tank(1.5), right = tank(12)
            // The pipe between the tanks, low like the mark's.
            ink.withAlphaComponent(0.8).setStroke()
            let pipe = NSBezierPath()
            pipe.move(to: NSPoint(x: left.maxX, y: 5)); pipe.line(to: NSPoint(x: right.minX, y: 5))
            pipe.lineWidth = 1
            pipe.stroke()
            for (box, used, hot) in [(left, summary.left, summary.deficit), (right, summary.right, false)] {
                let path = NSBezierPath(roundedRect: box, xRadius: 2.2, yRadius: 2.2)
                if let used {
                    NSGraphicsContext.saveGraphicsState()
                    path.addClip()
                    let level = box.height * CGFloat(min(100, max(0, used)) / 100)
                    (hot ? CF.humanNS : ink).setFill()
                    NSRect(x: box.minX, y: box.minY, width: box.width, height: max(used > 0 ? 1.5 : 0, level)).fill()
                    NSGraphicsContext.restoreGraphicsState()
                }
                ink.setStroke()
                path.lineWidth = 1.1
                path.stroke()
            }
            return true
        }
        image.isTemplate = false
        image.accessibilityDescription = summary.tooltip
        return image
    }
}
