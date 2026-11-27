// Offscreen composition for review images. The menu itself is the real SwiftUI view in an
// NSHostingView; only the wallpaper, the menu bar strip and the panel edge are drawn here, because a
// menu's system chrome (blur, rounded edge, shadow) exists only inside a live NSMenu.
import AppKit
import CrossfeedCore
import CrossfeedUI
import SwiftUI

enum Compose {
    static func bitmap(points: NSSize, scale: CGFloat, draw: () -> Void) -> NSImage {
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(points.width * scale),
                                   pixelsHigh: Int(points.height * scale), bitsPerSample: 8, samplesPerPixel: 4,
                                   hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        rep.size = points
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        draw()
        NSGraphicsContext.restoreGraphicsState()
        let image = NSImage(size: points)
        image.addRepresentation(rep)
        return image
    }

    /// The menu's content view, drawn the way the live menu draws it.
    @MainActor
    static func menuImage(state: MenuState, appearance: NSAppearance, scale: CGFloat, showsActions: Bool = true,
                          prepare: ((MenuState) -> Void)? = nil) -> NSImage {
        let host = NSHostingView(rootView: CrossfeedMenuView(state: state, showsActions: showsActions))
        host.appearance = appearance
        let fit = host.fittingSize
        host.frame = NSRect(origin: .zero, size: NSSize(width: MenuMetrics.width, height: ceil(fit.height)))
        let window = NSWindow(contentRect: host.frame, styleMask: .borderless, backing: .buffered, defer: false)
        window.appearance = appearance
        window.contentView = host
        host.layoutSubtreeIfNeeded()
        if let prepare {
            // One pass first, so every tile has reported where it is; then the change (a confirm, a drag), then draw.
            RunLoop.current.run(until: Date().addingTimeInterval(0.15))
            prepare(state)
            RunLoop.current.run(until: Date().addingTimeInterval(0.15))
            host.frame = NSRect(origin: .zero, size: NSSize(width: MenuMetrics.width, height: ceil(host.fittingSize.height)))
            host.layoutSubtreeIfNeeded()
        }
        let size = NSSize(width: MenuMetrics.width, height: ceil(host.frame.height))
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale), pixelsHigh: Int(size.height * scale),
                                   bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                   colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        rep.size = size
        host.cacheDisplay(in: host.bounds, to: rep)
        let image = NSImage(size: size)
        image.addRepresentation(rep)
        return image
    }

    /// A template image filled with one colour, the way the menu bar tints it.
    static func tinted(_ image: NSImage, _ color: NSColor) -> NSImage {
        let copy = NSImage(size: image.size)
        copy.lockFocus()
        image.draw(in: NSRect(origin: .zero, size: image.size))
        color.set()
        NSRect(origin: .zero, size: image.size).fill(using: .sourceAtop)
        copy.unlockFocus()
        return copy
    }

    /// Wallpaper, a menu bar strip with Crossfeed's item pressed, and the menu panel under it.
    static func screen(menu: NSImage, summary: BarSummary, appearance: NSAppearance, scale: CGFloat) -> NSImage {
        let dark = appearance.name == .darkAqua
        let margin: CGFloat = 24, barHeight: CGFloat = 31, gap: CGFloat = 6
        let size = NSSize(width: menu.size.width + margin * 2, height: barHeight + gap + menu.size.height + margin)
        return bitmap(points: size, scale: scale) {
            let rect = NSRect(origin: .zero, size: size)
            let top = dark ? NSColor(srgbRed: 0.16, green: 0.15, blue: 0.40, alpha: 1) : NSColor(srgbRed: 0.45, green: 0.45, blue: 0.92, alpha: 1)
            let bottom = dark ? NSColor(srgbRed: 0.09, green: 0.07, blue: 0.24, alpha: 1) : NSColor(srgbRed: 0.27, green: 0.22, blue: 0.74, alpha: 1)
            NSGradient(starting: top, ending: bottom)!.draw(in: rect, angle: -90)
            NSColor(srgbRed: 0.95, green: 0.78, blue: 0.95, alpha: dark ? 0.10 : 0.35).setFill()
            let streak = NSBezierPath()
            streak.move(to: NSPoint(x: 0, y: size.height * 0.62)); streak.line(to: NSPoint(x: size.width, y: size.height * 0.80))
            streak.line(to: NSPoint(x: size.width, y: size.height * 0.66)); streak.line(to: NSPoint(x: 0, y: size.height * 0.42))
            streak.fill()

            // Menu bar strip with Crossfeed's pressed item at its left, like CodexBar's screenshot.
            NSColor(white: dark ? 0 : 1, alpha: dark ? 0.28 : 0.22).setFill()
            NSRect(x: 0, y: size.height - barHeight, width: size.width, height: barHeight).fill()
            let icon = MenuBarIcon.image(for: summary)
            let label = summary.text as NSString
            let attrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium),
                .foregroundColor: summary.deficit ? CF.humanNS : NSColor.white]
            let labelSize = label.size(withAttributes: attrs)
            let iconWidth = icon?.size.width ?? 0
            let itemWidth = iconWidth + 12 + (label.length > 0 ? labelSize.width + (icon == nil ? -2 : 4) : 0)
            let item = NSRect(x: 18, y: size.height - barHeight + 4, width: itemWidth, height: barHeight - 8)
            NSColor(white: 1, alpha: 0.30).setFill()
            NSBezierPath(roundedRect: item, xRadius: 8, yRadius: 8).fill()
            NSAppearance(named: .darkAqua)!.performAsCurrentDrawingAppearance {
                if let icon {
                    let mark = icon.isTemplate ? tinted(icon, summary.deficit ? CF.humanNS : .white) : icon
                    mark.draw(in: NSRect(x: item.minX + 6, y: item.midY - icon.size.height / 2, width: icon.size.width, height: icon.size.height))
                }
                if label.length > 0 {
                    label.draw(at: NSPoint(x: item.minX + (icon == nil ? 6 : 10 + iconWidth), y: item.midY - labelSize.height / 2), withAttributes: attrs)
                }
            }

            // Panel: system-like material, then the real content on top.
            let panel = NSRect(x: margin, y: margin, width: menu.size.width, height: menu.size.height)
            let path = NSBezierPath(roundedRect: panel, xRadius: 12, yRadius: 12)
            NSGraphicsContext.saveGraphicsState()
            let shadow = NSShadow()
            shadow.shadowColor = NSColor(white: 0, alpha: 0.35); shadow.shadowBlurRadius = 14; shadow.shadowOffset = NSSize(width: 0, height: -4)
            shadow.set()
            (dark ? NSColor(white: 0.10, alpha: 1) : NSColor(white: 0.97, alpha: 1)).setFill()
            path.fill()
            NSGraphicsContext.restoreGraphicsState()
            NSGraphicsContext.saveGraphicsState()
            path.addClip()
            (dark ? NSColor(white: 0.10, alpha: 0.62) : NSColor(white: 0.97, alpha: 0.62)).setFill()
            panel.fill()
            menu.draw(in: panel)
            NSGraphicsContext.restoreGraphicsState()
            (dark ? NSColor(white: 1, alpha: 0.18) : NSColor(white: 0, alpha: 0.18)).setStroke()
            path.lineWidth = 0.5
            path.stroke()
        }
    }

    /// Screenshots at the same scale (2 px per point), titled, top aligned. Pixel size / scale = points.
    static func row(_ panels: [(title: String, image: NSImage)], scale: CGFloat) -> NSImage {
        let sizes = panels.map { panel -> NSSize in
            let rep = panel.image.representations.first
            return NSSize(width: (rep.map { CGFloat($0.pixelsWide) } ?? panel.image.size.width * scale) / scale,
                          height: (rep.map { CGFloat($0.pixelsHigh) } ?? panel.image.size.height * scale) / scale)
        }
        let title: CGFloat = 36, gap: CGFloat = 20, pad: CGFloat = 16
        let size = NSSize(width: pad * 2 + sizes.map(\.width).reduce(0, +) + gap * CGFloat(panels.count - 1),
                          height: pad * 2 + title + (sizes.map(\.height).max() ?? 0))
        return bitmap(points: size, scale: scale) {
            NSColor(white: 0.93, alpha: 1).setFill()
            NSRect(origin: .zero, size: size).fill()
            let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 13, weight: .semibold), .foregroundColor: NSColor.black]
            let top = size.height - pad
            var x = pad
            for (panel, box) in zip(panels, sizes) {
                (panel.title as NSString).draw(at: NSPoint(x: x, y: top - 20), withAttributes: attrs)
                panel.image.draw(in: NSRect(x: x, y: top - title - box.height, width: box.width, height: box.height))
                x += box.width + gap
            }
        }
    }

    /// Menu bar items on a plain light and a plain dark bar, one row per look, labelled in the margin.
    static func strips(_ looks: [(title: String, summary: BarSummary)], scale: CGFloat) -> NSImage {
        let rowHeight: CGFloat = 28, labelWidth: CGFloat = 188, stripWidth: CGFloat = 300, pad: CGFloat = 12
        let size = NSSize(width: pad * 2 + labelWidth + stripWidth * 2 + 8,
                          height: pad * 2 + 22 + rowHeight * CGFloat(looks.count))
        return bitmap(points: size, scale: scale) {
            NSColor(white: 0.97, alpha: 1).setFill()
            NSRect(origin: .zero, size: size).fill()
            let head: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 11, weight: .semibold), .foregroundColor: NSColor.darkGray]
            ("Light menu bar" as NSString).draw(at: NSPoint(x: pad + labelWidth, y: size.height - pad - 14), withAttributes: head)
            ("Dark menu bar" as NSString).draw(at: NSPoint(x: pad + labelWidth + stripWidth + 8, y: size.height - pad - 14), withAttributes: head)
            for (index, look) in looks.enumerated() {
                let y = size.height - pad - 22 - rowHeight * CGFloat(index + 1)
                (look.title as NSString).draw(at: NSPoint(x: pad, y: y + 8),
                    withAttributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.black])
                for (column, dark) in [false, true].enumerated() {
                    let x = pad + labelWidth + (stripWidth + 8) * CGFloat(column)
                    (dark ? NSColor(srgbRed: 0.17, green: 0.16, blue: 0.22, alpha: 1) : NSColor(srgbRed: 0.90, green: 0.89, blue: 0.94, alpha: 1)).setFill()
                    NSRect(x: x, y: y, width: stripWidth, height: rowHeight - 2).fill()
                    let ink: NSColor = dark ? .white : .black
                    let appearance = NSAppearance(named: dark ? .darkAqua : .aqua)!
                    appearance.performAsCurrentDrawingAppearance {
                        var cursor = x + 10
                        if let icon = MenuBarIcon.image(for: look.summary) {
                            let mark = icon.isTemplate ? tinted(icon, look.summary.deficit ? CF.humanNS : ink) : icon
                            mark.draw(in: NSRect(x: cursor, y: y + (rowHeight - 2) / 2 - icon.size.height / 2, width: icon.size.width, height: icon.size.height))
                            cursor += icon.size.width + 4
                        }
                        if !look.summary.text.isEmpty {
                            let attrs: [NSAttributedString.Key: Any] = [
                                .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium),
                                .foregroundColor: look.summary.deficit ? CF.humanNS : ink]
                            (look.summary.text as NSString).draw(at: NSPoint(x: cursor, y: y + 6), withAttributes: attrs)
                        }
                    }
                }
            }
        }
    }
}
