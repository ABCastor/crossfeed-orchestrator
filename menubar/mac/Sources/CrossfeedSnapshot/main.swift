// Renders Crossfeed's open menu offscreen, with no screen, pointer or focus involved:
// the very NSHostingView the app puts in its menu is drawn into a bitmap.
//
//   CrossfeedSnapshot --fixture glance.json --now 2026-11-25T09:53:57Z --fonts Resources/Fonts \
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
if let fonts = argument("--fonts") { _ = CF.registerFonts(in: URL(fileURLWithPath: fonts)) }
let now = argument("--now").flatMap { ISO8601DateFormatter().date(from: $0) } ?? Date()
let dark = argument("--appearance") == "dark"
let appearance = NSAppearance(named: dark ? .darkAqua : .aqua)!

MainActor.assumeIsolated {
do {
    let glance = try Glance.decode(Data(contentsOf: URL(fileURLWithPath: fixture)))
    let presenter = Presenter(glance: glance, loadedAt: now, now: now)
    let state = MenuState(presenter: presenter, selected: argument("--tab"))
    let scale: CGFloat = 2
    let menu = Compose.menuImage(state: state, appearance: appearance, scale: scale)
    var final = Compose.screen(menu: menu, summary: presenter.summary, appearance: appearance, scale: scale)
    if let reference = argument("--side-by-side") {
        guard let other = NSImage(contentsOfFile: reference) else { throw GlanceError.malformed }
        final = Compose.sideBySide(left: other, right: final, leftTitle: "CodexBar 0.67.0 (MIT), README screenshot",
                                   rightTitle: "Crossfeed, fixture glance JSON", scale: scale)
    }
    guard let tiff = final.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
    try png.write(to: URL(fileURLWithPath: out))
    print(out)
} catch {
    FileHandle.standardError.write(Data("snapshot failed: \(error)\n".utf8))
    exit(1)
}
}
