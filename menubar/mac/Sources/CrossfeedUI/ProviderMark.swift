import AppKit
import CrossfeedCore
import SwiftUI

public enum ProviderAssets {
    fileprivate static var images: [ProviderLogo: NSImage] = [:]

    /// The app and offscreen renderer use the same vetted asset directory. No network or fallback glyphs.
    @discardableResult
    public static func load(from directory: URL) -> Int {
        images = Dictionary(uniqueKeysWithValues: ProviderLogo.allCases.compactMap { logo in
            guard let image = NSImage(contentsOf: directory.appendingPathComponent(logo.rawValue + ".svg")),
                  image.isValid else { return nil }
            switch logo {
            case .openai, .copilot, .opencode, .openrouter: image.isTemplate = true
            case .claude, .gemini, .antigravity: image.isTemplate = false
            }
            return (logo, image)
        })
        return images.count
    }

    /// The mark for the menu bar: one-colour marks stay templates (the bar tints them to its ink), colour marks keep their colour.
    public static func menuBarImage(_ logo: ProviderLogo, height: CGFloat) -> NSImage? {
        guard let source = images[logo], let image = source.copy() as? NSImage else { return nil }
        let ratio = source.size.height > 0 ? source.size.width / source.size.height : 1
        image.size = NSSize(width: (height * ratio).rounded(), height: height)
        image.isTemplate = source.isTemplate
        return image
    }
}

struct ProviderMark: View {
    let logo: ProviderLogo
    var size: CGFloat = 16

    var body: some View {
        if let image = ProviderAssets.images[logo] {
            Image(nsImage: image).renderingMode(image.isTemplate ? .template : .original)
                .resizable().scaledToFit().frame(width: size, height: size)
                .foregroundStyle(CF.ink).accessibilityHidden(true)
        }
    }
}
