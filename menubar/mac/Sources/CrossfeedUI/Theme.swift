// Crossfeed menu bar: colours and type from the console (scripts/console-assets/console.css).
// Paper and ink; teal is the machine following its gauges; terracotta is the hand or a deficit;
// olive is allowance about to expire, which both should want spent.
import AppKit
import CoreText
import CrossfeedCore
import SwiftUI

public typealias PaceKindUI = PaceKind

public enum CF {
    static func dynamic(light: UInt32, dark: UInt32, alpha: CGFloat = 1) -> NSColor {
        NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            let hex = isDark ? dark : light
            return NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255, green: CGFloat((hex >> 8) & 255) / 255,
                           blue: CGFloat(hex & 255) / 255, alpha: alpha)
        }
    }

    /// Colours as the B+ Tiles tokens give them (design-r11 theme.js): light and dark, alpha included.
    static func rgba(_ light: (UInt32, CGFloat), _ dark: (UInt32, CGFloat)) -> NSColor {
        NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            let (hex, alpha) = isDark ? dark : light
            return NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255, green: CGFloat((hex >> 8) & 255) / 255,
                           blue: CGFloat(hex & 255) / 255, alpha: alpha)
        }
    }

    public static let paperNS = dynamic(light: 0xFAF9F4, dark: 0x1B1A17)
    public static let sunkNS = dynamic(light: 0xEFEDE6, dark: 0x131210)
    public static let tileNS = dynamic(light: 0xFFFFFF, dark: 0x22211D)
    public static let inkNS = dynamic(light: 0x14130F, dark: 0xECE9E2)
    public static let quietNS = dynamic(light: 0x5E594E, dark: 0xB4AFA6)
    public static let faintNS = dynamic(light: 0x8A8478, dark: 0x8C877E)
    public static let ruleNS = dynamic(light: 0xE2DDD2, dark: 0x36332D)
    static let ruleStrongNS = dynamic(light: 0x8C877C, dark: 0x777268)
    public static let surfaceNS = dynamic(light: 0xF1EEE6, dark: 0x2C2A25)
    public static let tileEdgeNS = rgba((0x14130F, 0.07), (0xECE9E2, 0.06))
    public static let trackNS = dynamic(light: 0xE6E2D8, dark: 0x3A3731)
    public static let machineNS = dynamic(light: 0x006770, dark: 0x00858F)
    public static let machineTextNS = dynamic(light: 0x006770, dark: 0x5DB3BA)
    public static let tealWashNS = rgba((0x006770, 0.11), (0x48A2AA, 0.16))
    public static let humanNS = dynamic(light: 0xC4552A, dark: 0xD9693D)
    public static let humanTextNS = dynamic(light: 0xA2441F, dark: 0xEC9672)
    public static let betweenNS = dynamic(light: 0x667D2F, dark: 0x8FA84F)
    public static let betweenTextNS = dynamic(light: 0x4F6322, dark: 0xB3C97A)
    public static let spareNS = dynamic(light: 0xA3BD5E, dark: 0x7D9545)
    public static let hatchNS = rgba((0xFFFFFF, 0.34), (0x1B1A17, 0.30))
    public static let markerNS = dynamic(light: 0x14130F, dark: 0xECE9E2)
    public static let buttonNS = dynamic(light: 0xEAE7DE, dark: 0x34322D)
    public static let buttonHoverNS = dynamic(light: 0xE0DCD1, dark: 0x3E3B35)
    public static let segmentOnNS = dynamic(light: 0xFFFFFF, dark: 0x4A4740)
    public static let hoverNS = rgba((0x14130F, 0.06), (0xECE9E2, 0.07))
    public static let sparkNS = dynamic(light: 0xC9C3B6, dark: 0x4A4740)
    public static let liftShadowNS = rgba((0x14130F, 0.16), (0x000000, 0.6))

    public static let paper = Color(nsColor: paperNS)
    public static let sunk = Color(nsColor: sunkNS)
    public static let tile = Color(nsColor: tileNS)
    public static let ink = Color(nsColor: inkNS)
    public static let quiet = Color(nsColor: quietNS)
    public static let faint = Color(nsColor: faintNS)
    public static let rule = Color(nsColor: ruleNS)
    static let ruleStrong = Color(nsColor: ruleStrongNS)
    public static let surface = Color(nsColor: surfaceNS)
    public static let tileEdge = Color(nsColor: tileEdgeNS)
    public static let track = Color(nsColor: trackNS)
    public static let machine = Color(nsColor: machineNS)
    public static let machineText = Color(nsColor: machineTextNS)
    public static let tealWash = Color(nsColor: tealWashNS)
    public static let human = Color(nsColor: humanNS)
    public static let humanText = Color(nsColor: humanTextNS)
    public static let between = Color(nsColor: betweenNS)
    public static let betweenText = Color(nsColor: betweenTextNS)
    public static let spare = Color(nsColor: spareNS)
    public static let hatch = Color(nsColor: hatchNS)
    public static let marker = Color(nsColor: markerNS)
    public static let button = Color(nsColor: buttonNS)
    public static let buttonHover = Color(nsColor: buttonHoverNS)
    public static let segmentOn = Color(nsColor: segmentOnNS)
    public static let hover = Color(nsColor: hoverNS)
    public static let spark = Color(nsColor: sparkNS)
    public static let liftShadow = Color(nsColor: liftShadowNS)

    // The system face, as the design's -apple-system: figures use tabular digits so columns line up.
    public static func face(_ size: CGFloat, _ weight: Weight = .regular) -> Font {
        .system(size: size, weight: weight.system)
    }
    public static func machineFont(_ size: CGFloat, _ weight: MonoWeight = .regular) -> Font {
        .system(size: size, weight: weight == .regular ? .regular : .medium).monospacedDigit()
    }
    public enum Weight: String {
        case regular = "Regular", medium = "Medium", semibold = "SemiBold"
        var system: Font.Weight { self == .regular ? .regular : (self == .medium ? .medium : .semibold) }
    }
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
