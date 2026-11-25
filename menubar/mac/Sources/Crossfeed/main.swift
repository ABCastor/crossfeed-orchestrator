// Crossfeed menu bar app for macOS. It shows what `fleetctl glance --json --no-refresh` says and nothing
// else: no provider readers, cookies, tokens or network calls live in this process.
//
// The menu is CodexBar's structure (MIT, Copyright (c) 2026 Peter Steinberger): a status item whose
// NSMenu holds one hosted SwiftUI card (provider tiles, usage bars, pace marker) above native action rows.
import AppKit
import CrossfeedCore
import CrossfeedUI
import SwiftUI

struct Config {
    var python = "python3"
    var fleetctl = ""
    var overlay = ""
    var state = ""
    var fixture = ""
    var fonts = ""
    var selftest = false
}

/// Accepts the LaunchAgent's Go-style flags (-python ...) and their double-dash forms.
func parse(_ args: [String]) -> Config {
    var c = Config()
    var i = 1
    while i < args.count {
        let name = args[i].drop(while: { $0 == "-" })
        let next = i + 1 < args.count ? args[i + 1] : ""
        switch name {
        case "python": c.python = next; i += 1
        case "fleetctl": c.fleetctl = next; i += 1
        case "overlay": c.overlay = next; i += 1
        case "state-dir": c.state = next; i += 1
        case "fixture": c.fixture = next; i += 1
        case "fonts": c.fonts = next; i += 1
        case "selftest": c.selftest = true
        default: break
        }
        i += 1
    }
    return c
}

enum Reader {
    /// One fleetctl call off the main thread, 8 s at most, output capped at 2 MB, schema 1 only.
    static func load(_ c: Config) -> Result<Glance, Error> {
        if !c.fixture.isEmpty {
            return Result { try Glance.decode(Data(contentsOf: URL(fileURLWithPath: c.fixture))) }
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: c.python.hasPrefix("/") ? c.python : "/usr/bin/env")
        var args = c.python.hasPrefix("/") ? [] : [c.python]
        args.append(c.fleetctl)
        if !c.overlay.isEmpty { args += ["--overlay", c.overlay] }
        if !c.state.isEmpty { args += ["--state-dir", c.state] }
        args += ["glance", "--json", "--no-refresh"]
        process.arguments = args
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return .failure(error) }
        let watchdog = DispatchWorkItem { if process.isRunning { process.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + 8, execute: watchdog)
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        watchdog.cancel()
        guard process.terminationStatus == 0, data.count <= 2 << 20 else { return .failure(GlanceError.malformed) }
        return Result { try Glance.decode(data) }
    }
}

/// Routes a click on a provider tile. A menu's tracking loop hands mouse events to the view itself;
/// the frames SwiftUI reported say which tile was hit.
final class MenuHostingView: NSHostingView<CrossfeedMenuView> {
    let model: MenuState

    init(model: MenuState) {
        self.model = model
        super.init(rootView: CrossfeedMenuView(state: model, showsActions: false))
    }

    @available(*, unavailable) required init(rootView: CrossfeedMenuView) { fatalError() }
    @available(*, unavailable) required init?(coder: NSCoder) { fatalError() }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseUp(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        let point = CGPoint(x: p.x, y: isFlipped ? p.y : bounds.height - p.y)
        if let hit = model.tileFrames.first(where: { $0.value.contains(point) }) {
            model.onSelect(hit.key)
        } else {
            super.mouseUp(with: event)
        }
    }
}

final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let config: Config
    let state = MenuState()
    let menu = NSMenu()
    var item: NSStatusItem?
    var glance: Glance?
    var loadedAt = Date()
    var loading = false
    var timers: [Timer] = []
    static let tabKey = "crossfeed.tab"

    init(config: Config) { self.config = config }

    func applicationDidFinishLaunching(_ notification: Notification) {
        state.onSelect = { [weak self] id in self?.select(id) }
        state.onOpenConsole = { Self.openConsole() }
        state.onReload = { [weak self] in self?.reload() }
        state.onAbout = { Self.about() }
        state.onQuit = { NSApp.terminate(nil) }
        state.failure = "Reading quotas…"
        menu.delegate = self
        menu.autoenablesItems = false
        rebuildMenu()
        if config.selftest { selftest(); return }
        let status = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        status.menu = menu
        item = status
        paintStatusItem()
        reload()
        timers.append(Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.reload() })
        timers.append(Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in self?.tick() })
    }

    // MARK: Data

    func reload() {
        guard !loading else { return }
        loading = true
        let c = config
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = Reader.load(c)
            DispatchQueue.main.async { self?.loaded(result) }
        }
    }

    func loaded(_ result: Result<Glance, Error>) {
        loading = false
        switch result {
        case .success(let data):
            glance = data
            loadedAt = Date()
            state.failure = nil
        case .failure:
            // Keep showing the last reading: its age grows in the header instead of vanishing.
            if glance == nil { state.failure = "Quotas unavailable" }
        }
        tick()
    }

    /// Rebuilds the presenter against the clock so countdowns and ages move without a new reading.
    func tick() {
        if let glance {
            let presenter = Presenter(glance: glance, loadedAt: loadedAt, now: Date())
            state.presenter = presenter
            let saved = UserDefaults.standard.string(forKey: Self.tabKey)
            let ids = presenter.tabs.map(\.id)
            if let current = state.selected, ids.contains(current) {
            } else {
                state.selected = saved.flatMap { ids.contains($0) ? $0 : nil } ?? presenter.defaultTab
            }
        } else {
            state.presenter = nil
        }
        paintStatusItem()
        if !menuIsOpen { rebuildMenu() }
    }

    func select(_ id: String) {
        guard id != state.selected else { return }
        state.selected = id
        UserDefaults.standard.set(id, forKey: Self.tabKey)
        rebuildMenu()
    }

    // MARK: Status item and menu

    func paintStatusItem() {
        guard let button = item?.button else { return }
        let summary = state.presenter?.summary
            ?? BarSummary(text: "", left: nil, right: nil, deficit: false, tooltip: "Crossfeed · reading quotas")
        button.image = MenuBarIcon.image(for: summary)
        button.imagePosition = .imageLeft
        var attrs: [NSAttributedString.Key: Any] = [.font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)]
        if summary.deficit { attrs[.foregroundColor] = CF.humanNS }
        button.attributedTitle = NSAttributedString(string: summary.text.isEmpty ? "" : " " + summary.text, attributes: attrs)
        button.toolTip = summary.tooltip
        button.setAccessibilityLabel(summary.tooltip)
    }

    var menuIsOpen = false

    func menuWillOpen(_ menu: NSMenu) {
        menuIsOpen = true
        reload()
    }

    func menuDidClose(_ menu: NSMenu) {
        menuIsOpen = false
        rebuildMenu()
    }

    func rebuildMenu() {
        menu.removeAllItems()
        let host = MenuHostingView(model: state)
        host.frame = NSRect(x: 0, y: 0, width: MenuMetrics.width, height: ceil(host.fittingSize.height))
        let card = NSMenuItem()
        card.view = host
        menu.addItem(card)
        menu.addItem(.separator())
        menu.addItem(action("Open console", symbol: "arrow.up.right.square", #selector(openConsole)))
        menu.addItem(action("Reload view", symbol: "arrow.clockwise", #selector(reloadView)))
        menu.addItem(.separator())
        menu.addItem(action("About Crossfeed", symbol: nil, #selector(about)))
        menu.addItem(action("Quit Crossfeed", symbol: nil, #selector(quit), key: "q"))
    }

    func action(_ title: String, symbol: String?, _ selector: Selector, key: String = "") -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: selector, keyEquivalent: key)
        entry.target = self
        entry.attributedTitle = NSAttributedString(string: title, attributes: [
            .font: NSFont(name: "Commissioner-Regular", size: 13) ?? NSFont.menuFont(ofSize: 0)])
        if let symbol { entry.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title) }
        return entry
    }

    @objc func openConsole() { Self.openConsole() }
    @objc func reloadView() { reload() }
    @objc func about() { Self.about() }
    @objc func quit() { NSApp.terminate(nil) }

    static func openConsole() {
        let url = "http://127.0.0.1:8768/"
        let launcher = NSHomeDirectory() + "/Applications/Crossfeed Orchestrator.app"
        var isDirectory: ObjCBool = false
        // The launcher app asks the console for a signed-in tab; the bare URL is the fallback.
        if FileManager.default.fileExists(atPath: launcher, isDirectory: &isDirectory), isDirectory.boolValue {
            NSWorkspace.shared.open(URL(fileURLWithPath: launcher))
        } else if let target = URL(string: url) {
            NSWorkspace.shared.open(target)
        }
    }

    static func about() {
        let credits = NSAttributedString(string: "Reads fleetctl glance only; no provider logins, cookies or network.\nMenu design adapted from CodexBar (MIT, Peter Steinberger).")
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "Crossfeed", .credits: credits])
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: Self test (builds the menu without a status item, shows nothing)

    func selftest() {
        let load = Reader.load(config)
        guard case .success(let data) = load else { print("selftest: cannot read the fixture"); exit(1) }
        glance = data
        tick()
        let host = MenuHostingView(model: state)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: MenuMetrics.width, height: 600), styleMask: .borderless,
                              backing: .buffered, defer: false)
        window.contentView = host
        host.frame = NSRect(x: 0, y: 0, width: MenuMetrics.width, height: ceil(host.fittingSize.height))
        host.layoutSubtreeIfNeeded()
        DispatchQueue.main.async {
            let tiles = self.state.tileFrames.count
            let fonts = NSFont(name: "Commissioner-Medium", size: 13) != nil && NSFont(name: "MartianMono-Regular", size: 13) != nil
            let ok = fonts && host.frame.height > 150 && tiles == (self.state.presenter?.tabs.count ?? -1) && self.menu.items.count == 7
            print("selftest \(ok ? "ok" : "FAILED"): fonts \(fonts), menu items \(self.menu.items.count), tiles \(tiles), card height \(Int(host.frame.height))")
            exit(ok ? 0 : 1)
        }
    }
}

let config = parse(CommandLine.arguments)
if !config.selftest && config.fixture.isEmpty && !config.fleetctl.hasPrefix("/") {
    FileHandle.standardError.write(Data("Set -fleetctl to the absolute path to fleetctl.py\n".utf8))
    exit(2)
}
let bundled = Bundle.main.resourceURL?.appendingPathComponent("Fonts")
CF.registerFonts(in: config.fonts.isEmpty ? (bundled ?? URL(fileURLWithPath: "/nonexistent")) : URL(fileURLWithPath: config.fonts))
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
let delegate = App(config: config)
application.delegate = delegate
application.run()
