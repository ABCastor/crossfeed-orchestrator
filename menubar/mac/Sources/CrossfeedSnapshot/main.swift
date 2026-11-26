// Renders Crossfeed's open menu offscreen, with no screen, pointer or focus involved:
// the very NSHostingView the app puts in its menu is drawn into a bitmap.
//
//   CrossfeedSnapshot --fixture glance.json --now <ISO time> --fonts Resources/Fonts \
//       --out menu.png [--appearance light|dark] [--tab codex] [--side-by-side codexbar.png]
import AppKit
import CrossfeedCore
import CrossfeedUI
import SwiftUI

func argument(_ name: String) -> String? {
    guard let i = CommandLine.arguments.firstIndex(of: name), i + 1 < CommandLine.arguments.count else { return nil }
    return CommandLine.arguments[i + 1]
}

guard let fixture = argument("--fixture"), let out = argument("--out") else {
    FileHandle.standardError.write(Data("usage: CrossfeedSnapshot --fixture F --out PNG [--now ISO] [--fonts DIR] [--appearance light|dark] [--tab ID] [--side-by-side PNG]\n".utf8))
    exit(2)
}
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let fonts = URL(fileURLWithPath: argument("--fonts") ?? "Resources/Fonts")
CF.registerFonts(in: fonts)
let logos = argument("--logos").map { URL(fileURLWithPath: $0) }
    ?? fonts.deletingLastPathComponent().appendingPathComponent("ProviderLogos")
guard ProviderAssets.load(from: logos) == ProviderLogo.allCases.count else {
    FileHandle.standardError.write(Data("snapshot: bundled provider logos not found; set --logos DIR\n".utf8))
    exit(2)
}
let now = argument("--now").flatMap { ISO8601DateFormatter().date(from: $0) } ?? Date()
let dark = argument("--appearance") == "dark"
let appearance = NSAppearance(named: dark ? .darkAqua : .aqua)!

MainActor.assumeIsolated {
do {
    let glance = try Glance.decode(Data(contentsOf: URL(fileURLWithPath: fixture)))
    let presenter = Presenter(glance: glance, loadedAt: now, now: now,
                              resetClock: argument("--reset") == "clock", timeZone: TimeZone(identifier: argument("--zone") ?? "Europe/Rome") ?? .current)
    let scale: CGFloat = 2
    var prefs = BarPrefs()
    if let v = argument("--show") { prefs.show = v }
    if let v = argument("--window") { prefs.window = v }
    if let v = argument("--count").flatMap(BarPrefs.Count.init) { prefs.count = v }
    if let v = argument("--icon").flatMap(BarPrefs.Icon.init) { prefs.icon = v }
    if let v = argument("--text").flatMap(BarPrefs.Text.init) { prefs.text = v }
    if let v = argument("--extra").flatMap(BarPrefs.Extra.init) { prefs.extra = v }
    if let v = argument("--reset").flatMap(BarPrefs.Reset.init) { prefs.reset = v }
    let settings = CommandLine.arguments.contains("--settings")
    if let stripsOut = argument("--strips-out") {
        // One row per look: the bar item alone, on a light and a dark bar.
        var looks: [(title: String, summary: BarSummary)] = []
        for (title, change) in [
            ("Tanks: just the percent", { (p: inout BarPrefs) in p.icon = .tanks; p.text = .number }),
            ("Tanks + provider, window, percent", { (p: inout BarPrefs) in p.icon = .tanks; p.text = .full }),
            ("Tanks + window, percent", { (p: inout BarPrefs) in p.icon = .tanks }),
            ("Logo + window, percent (default)", { (p: inout BarPrefs) in }),
            ("Logo + provider, window, percent", { (p: inout BarPrefs) in p.text = .full }),
            ("Logo + percent (compact)", { (p: inout BarPrefs) in p.icon = .logo; p.text = .number }),
            ("Tanks only", { (p: inout BarPrefs) in p.icon = .tanks; p.text = .none }),
            ("Text only, what is left", { (p: inout BarPrefs) in p.icon = .none; p.count = .left }),
            ("Add: reset", { (p: inout BarPrefs) in p.extra = .reset }),
            ("Add: reset, as a clock", { (p: inout BarPrefs) in p.extra = .reset; p.reset = .clock }),
            ("Add: pace", { (p: inout BarPrefs) in p.extra = .pace }),
            ("Add: cost today", { (p: inout BarPrefs) in p.extra = .cost }),
            ("Logo + percent + pace", { (p: inout BarPrefs) in p.icon = .logo; p.text = .number; p.extra = .pace }),
        ] as [(String, (inout BarPrefs) -> Void)] {
            var p = prefs
            change(&p)
            let source = p.reset == .clock ? Presenter(glance: glance, loadedAt: now, now: now, resetClock: true,
                                                       timeZone: TimeZone(identifier: argument("--zone") ?? "Europe/Rome") ?? .current) : presenter
            looks.append((title, source.barSummary(p)))
        }
        let image = Compose.strips(looks, scale: 2)
        guard let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
        try png.write(to: URL(fileURLWithPath: stripsOut))
        print(stripsOut)
        exit(0)
    }
    let tabs = (argument("--tabs") ?? argument("--tab") ?? "").split(separator: ",").map(String.init)
    var panels: [(title: String, image: NSImage)] = []
    // One panel height for every tab, as the live menu uses (--no-fixed draws the old content-sized menu).
    let heights = PanelSizing.measure(presenter: presenter, prefs: prefs, cap: argument("--cap").flatMap { Double($0) }.map { CGFloat($0) } ?? 760)
    if CommandLine.arguments.contains("--heights") {
        print("panel \(Int(heights.panel)) settings \(Int(heights.settings)) tabs " + heights.perTab.sorted { $0.key < $1.key }.map { "\($0.key) \(Int($0.value))" }.joined(separator: " "))
    }
    for tab in tabs.isEmpty ? [presenter.defaultTab ?? ""] : tabs {
        let state = MenuState(presenter: presenter, selected: tab.isEmpty ? nil : tab)
        state.prefs = prefs
        if !CommandLine.arguments.contains("--no-fixed") { state.panelHeight = heights.panel }
        state.showsSettings = settings
        let menu = Compose.menuImage(state: state, appearance: appearance, scale: scale)
        let screen = Compose.screen(menu: menu, summary: presenter.barSummary(prefs), appearance: appearance, scale: scale)
        let name = presenter.tabs.first { $0.id == state.selected }?.title ?? "menu"
        panels.append(("Crossfeed, \(name) tab, fixture glance JSON", screen))
    }
    var all: [(title: String, image: NSImage)] = []
    if let reference = argument("--side-by-side") {
        guard let other = NSImage(contentsOfFile: reference) else { throw GlanceError.malformed }
        all.append((argument("--reference-title") ?? "CodexBar 0.67.0 (MIT), README screenshot", other))
    }
    all += panels
    let final = all.count == 1 ? all[0].image : Compose.row(all, scale: scale)
    guard let tiff = final.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
    try png.write(to: URL(fileURLWithPath: out))
    print(out)
} catch {
    FileHandle.standardError.write(Data("snapshot failed: \(error)\n".utf8))
    exit(1)
}
}
