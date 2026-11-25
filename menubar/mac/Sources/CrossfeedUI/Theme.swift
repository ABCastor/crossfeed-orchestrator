// Crossfeed menu bar: colours and type from the console (scripts/console-assets/console.css).
// Paper and ink; teal is the machine following its gauges; terracotta is the hand or a deficit;
// olive is allowance about to expire, which both should want spent.
import AppKit
import CoreText
import SwiftUI

public enum CF {
    static func dynamic(light: UInt32, dark: UInt32, alpha: CGFloat = 1) -> NSColor {
        NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            let hex = isDark ? dark : light
            return NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255, green: CGFloat((hex >> 8) & 255) / 255,
                           blue: CGFloat(hex & 255) / 255, alpha: alpha)
        }
    }

    public static let paperNS = dynamic(light: 0xFAF9F4, dark: 0x141311)
    public static let inkNS = dynamic(light: 0x14130F, dark: 0xE9E6DF)
    public static let quietNS = dynamic(light: 0x4D493F, dark: 0xBDB8B0)
    public static let ruleNS = dynamic(light: 0xDDD8CC, dark: 0x37342E)
    static let ruleStrongNS = dynamic(light: 0x8C877C, dark: 0x777268)
    public static let surfaceNS = dynamic(light: 0xF3F1E9, dark: 0x1F1E1A)
    public static let machineNS = dynamic(light: 0x006770, dark: 0x00858F)
    public static let machineTextNS = dynamic(light: 0x006770, dark: 0x48A2AA)
    public static let humanNS = dynamic(light: 0xC4552A, dark: 0xD9693D)
    public static let humanTextNS = dynamic(light: 0xA2441F, dark: 0xE08A66)
    public static let betweenNS = dynamic(light: 0x667D2F, dark: 0x8FA84F)

    public static let paper = Color(nsColor: paperNS)
    public static let ink = Color(nsColor: inkNS)
    public static let quiet = Color(nsColor: quietNS)
    public static let rule = Color(nsColor: ruleNS)
    static let ruleStrong = Color(nsColor: ruleStrongNS)
    public static let surface = Color(nsColor: surfaceNS)
    public static let machine = Color(nsColor: machineNS)
    public static let machineText = Color(nsColor: machineTextNS)
    public static let human = Color(nsColor: humanNS)
    public static let humanText = Color(nsColor: humanTextNS)
    public static let between = Color(nsColor: betweenNS)

    // Commissioner labels, Martian Mono measurement. Static weights cut from the console's fonts.
    // A face that is not registered falls back to the system font, so the menu never breaks.
    public static func face(_ size: CGFloat, _ weight: Weight = .regular) -> Font {
        .custom("Commissioner-\(weight.rawValue)", size: size)
    }
    public static func machineFont(_ size: CGFloat, _ weight: MonoWeight = .regular) -> Font {
        .custom("MartianMono-\(weight.rawValue)", size: size)
    }
    public enum Weight: String { case regular = "Regular", medium = "Medium", semibold = "SemiBold" }
    public enum MonoWeight: String { case regular = "Regular", medium = "Medium" }

    /// Registers the bundled .ttf files for this process. Returns how many registered.
    @discardableResult
    public static func registerFonts(in directory: URL) -> Int {
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        var count = 0
        for file in files where file.pathExtension == "ttf" {
            var error: Unmanaged<CFError>?
            if CTFontManagerRegisterFontsForURL(file as CFURL, .process, &error) { count += 1 }
        }
        return count
    }

    /// The dot beside a pace line: terracotta in deficit, olive in surplus.
    public static func dotColor(_ pace: PaceKindUI) -> Color { pace == .deficit ? human : paceColor(pace) }

    public static func paceColor(_ pace: PaceKindUI) -> Color {
        switch pace {
        case .deficit: return ink
        case .surplus: return between
        case .onPace: return machine
        case .unknown: return quiet
        }
    }
}
