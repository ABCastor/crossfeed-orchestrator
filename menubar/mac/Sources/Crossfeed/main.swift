// Crossfeed menu bar app for macOS. It reads fleetctl glance and saves operator spend changes through
// fleetctl level: no provider readers, cookies, tokens or network calls live in this process.
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
    var selftestTab: String?
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
        case "tab": c.selftestTab = next; i += 1
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

/// A menu's tracking loop hands mouse events to the host; SwiftUI reports which tile or segment was hit.
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
        if !model.routeClick(at: point) { super.mouseUp(with: event) }
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
    var revision = 0
    let io = DispatchQueue(label: "crossfeed.fleetctl", qos: .utility)
    var timers: [Timer] = []
    static let tabKey = "crossfeed.tab"

    init(config: Config) { self.config = config }

    func applicationDidFinishLaunching(_ notification: Notification) {
        state.onSelect = { [weak self] id in self?.select(id) }
        state.canEditPolicy = config.fixture.isEmpty || config.selftest
        state.onPolicyChange = { [weak self] change in self?.changePolicy(change) }
        state.onOpenConsole = { Self.openConsole() }
        state.onReload = { [weak self] in self?.reload() }
        state.onAbout = { Self.about() }
        state.onQuit = { NSApp.terminate(nil) }
        state.failure = "Reading quotas…"
        if config.selftest { state.selected = config.selftestTab }
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
        guard !loading, state.edits.pending == nil else { return }
        loading = true
        let c = config, readingRevision = revision
        io.async { [weak self] in
            let result = Reader.load(c)
            RunLoop.main.perform(inModes: [.common, .eventTracking]) {
                guard let self else { return }
                self.loading = false
                if self.revision == readingRevision { self.acceptReading(result); self.tick() }
            }
        }
    }

    func changePolicy(_ change: PolicyChange) {
        guard config.fixture.isEmpty, !config.selftest,
              let pool = glance?.pools.first(where: { $0.pool == change.pool }),
              state.edits.begin(change, current: pool.level.flatMap(SpendLevel.init(rawValue:))) else { return }
        revision += 1 // do not paint a pre-write poll over this transaction
        let c = config
        let command = PolicyCommand(python: c.python, fleetctl: c.fleetctl, overlay: c.overlay, stateDir: c.state)
        io.async { [weak self] in
            let result = command.save(change)
            let refreshed: Glance? = (try? result.get()) != nil ? try? Reader.load(c).get() : nil
            // NSMenu has its own tracking run loop; publish the result while the menu is still open.
            RunLoop.main.perform(inModes: [.common, .eventTracking]) {
                guard let self else { return }
                if let refreshed {
                    self.acceptReading(.success(refreshed))
                } else if case .success = result, let index = self.glance?.pools.firstIndex(where: { $0.pool == change.pool }) {
                    // The CLI committed but the readback failed. Keep the saved selection without inventing attribution.
                    self.glance?.pools[index].level = change.level.rawValue
                    self.glance?.pools[index].levelControl = nil
                }
                self.state.edits.finish(result, readingAvailable: refreshed != nil)
                self.tick()
            }
        }
    }

    func acceptReading(_ result: Result<Glance, Error>) {
        switch result {
        case .success(let data):
            glance = data
            loadedAt = Date()
            state.failure = nil
        case .failure:
            // Keep showing the last reading: its age grows in the header instead of vanishing.
            if glance == nil { state.failure = "Quotas unavailable" }
        }
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
        if menuIsOpen { remeasure() } else { rebuildMenu() }
    }

    /// A new reading can change the card's height while the menu is open; AppKit does not resize a custom
    /// view, so swap the item for one of the right size when (and only when) the height changed.
    func remeasure() {
        RunLoop.main.perform(inModes: [.common, .eventTracking]) { [weak self] in
            guard let self, let host = self.host else { return }
            if abs(ceil(host.fittingSize.height) - host.frame.height) > 0.5 { self.rebuildMenu() }
        }
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
    var host: MenuHostingView?

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
        self.host = host
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
        let credits = NSAttributedString(string: "Reads quotas and saves spend policy through fleetctl. No provider logins, cookies or network.\nMenu design adapted from CodexBar (MIT, Peter Steinberger). Provider marks from Lobe Icons (MIT).")
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
            let policyFrames = self.state.policyFrames
            let policyPools = self.state.selected.flatMap { self.state.presenter?.card(for: $0) }?
                .sections.filter { $0.level != nil }.count ?? 0
            var hit: PolicyChange?
            self.state.onPolicyChange = { hit = $0 }
            var routeOK = policyPools == 0 && policyFrames.isEmpty
            if let (change, rect) = policyFrames.first {
                routeOK = self.state.routeClick(at: CGPoint(x: rect.midX, y: rect.midY)) && hit == change
            }
            let ok = fonts && host.frame.height > 150 && tiles == (self.state.presenter?.tabs.count ?? -1)
                && self.menu.items.count == 7 && routeOK && policyFrames.count == policyPools * SpendLevel.allCases.count
                && providerAssetCount == ProviderLogo.allCases.count
            print("selftest \(ok ? "ok" : "FAILED"): fonts \(fonts), logos \(providerAssetCount), menu items \(self.menu.items.count), tiles \(tiles), policy segments \(policyFrames.count), card height \(Int(host.frame.height))")
            exit(ok ? 0 : 1)
        }
    }
}

let config = parse(CommandLine.arguments)
if !config.selftest && config.fixture.isEmpty && !config.fleetctl.hasPrefix("/") {
    FileHandle.standardError.write(Data("Set -fleetctl to the absolute path to fleetctl.py\n".utf8))
    exit(2)
}
let resources = Bundle.main.resourceURL ?? URL(fileURLWithPath: "/nonexistent")
CF.registerFonts(in: config.fonts.isEmpty ? resources.appendingPathComponent("Fonts") : URL(fileURLWithPath: config.fonts))
let providerAssetCount = ProviderAssets.load(from: resources.appendingPathComponent("ProviderLogos"))
let application = NSApplication.shared
application.setActivationPolicy(config.selftest ? .prohibited : .accessory)
let delegate = App(config: config)
application.delegate = delegate
application.run()
