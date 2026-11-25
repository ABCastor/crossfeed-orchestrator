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

/// Route pointer tracking for the hosted tiles and dial; SwiftUI buttons still handle keyboard and accessibility.
final class MenuHostingView: NSHostingView<CrossfeedMenuView> {
    let model: MenuState

    init(model: MenuState) {
        self.model = model
        super.init(rootView: CrossfeedMenuView(state: model, showsActions: false))
    }

    @available(*, unavailable) required init(rootView: CrossfeedMenuView) { fatalError() }
    @available(*, unavailable) required init?(coder: NSCoder) { fatalError() }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    private func menuPoint(_ local: CGPoint) -> CGPoint {
        CGPoint(x: local.x, y: isFlipped ? local.y : bounds.height - local.y)
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        if model.ownsPointer(at: menuPoint(convert(point, from: superview))) { return self }
        return super.hitTest(point)
    }

    override func mouseDown(with event: NSEvent) {
        let point = menuPoint(convert(event.locationInWindow, from: nil))
        if model.ownsPointer(at: point) {
            model.beginPointer(at: point)
        } else {
            super.mouseDown(with: event)
        }
    }

    override func mouseDragged(with event: NSEvent) {
        model.movePointer(to: menuPoint(convert(event.locationInWindow, from: nil)))
    }

    override func mouseUp(with event: NSEvent) {
        if !model.endPointer(at: menuPoint(convert(event.locationInWindow, from: nil))) {
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
    var revision = 0
    let io = DispatchQueue(label: "crossfeed.fleetctl", qos: .utility)
    var timers: [Timer] = []
    static let tabKey = "crossfeed.tab"
    let tabOrder = TabOrder(defaults: .standard)
    let console: ConsoleSession

    init(config: Config) {
        self.config = config
        self.console = ConsoleSession(config: config)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        state.onSelect = { [weak self] id in self?.select(id) }
        state.canEditPolicy = config.fixture.isEmpty || config.selftest
        state.onPolicyChange = { [weak self] change in self?.changePolicy(change) }
        state.onReorder = { [weak self] source, target in self?.reorder(source, onto: target) }
        state.onOpenStatus = { [weak self] url in
            self?.menu.cancelTracking()
            NSWorkspace.shared.open(url)
        }
        state.onOpenConsole = { [weak self] in self?.openConsole() }
        state.onReload = { [weak self] in self?.reload() }
        state.onAbout = { [weak self] in self?.about() }
        state.onQuit = { NSApp.terminate(nil) }
        console.onFailure = { [weak self] failure in
            guard let self, self.state.actionFailure != failure else { return }
            self.state.actionFailure = failure
            if self.menuIsOpen { self.remeasure() } else { self.rebuildMenu() }
        }
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
              let level = glance?.policyLevel(for: change.pool),
              state.edits.begin(change, current: SpendLevel(rawValue: level), mixed: level == "mixed") else { return }
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
                } else if case .success = result {
                    self.glance?.applyConfirmed(change)
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
            let order = config.fixture.isEmpty && !config.selftest ? tabOrder.ordered(ids) : ids
            if order != state.providerOrder { state.providerOrder = order }
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

    func reorder(_ source: String, onto target: String) {
        guard config.fixture.isEmpty, !config.selftest, let ids = state.presenter?.tabs.map(\.id),
              tabOrder.move(source, onto: target, among: ids) else { return }
        state.providerOrder = tabOrder.ordered(ids)
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
        state.cancelPointer()
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

    @objc func openConsole() { console.open(.providers) }
    @objc func reloadView() { reload() }
    @objc func about() { console.open(.about) }
    @objc func quit() { NSApp.terminate(nil) }

    func applicationWillTerminate(_ notification: Notification) { console.stop() }

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
            let card = self.state.selected.flatMap { self.state.presenter?.card(for: $0) }
            let targets = (card?.sections.compactMap { $0.policy?.id } ?? []) + [card?.subscriptionPolicy?.id].compactMap { $0 }
            var hit: PolicyChange?
            self.state.onPolicyChange = { hit = $0 }
            let routeOK = targets.allSatisfy { id in
                SpendLevel.allCases.allSatisfy { level in
                    let change = PolicyChange(pool: id, level: level)
                    guard let rect = policyFrames[change] else { return false }
                    hit = nil
                    return self.state.endPointer(at: CGPoint(x: rect.midX, y: rect.midY)) && hit == change
                }
            }
            let dragOK = self.selftestDrag()
            var statusClick: URL?
            self.state.onOpenStatus = { statusClick = $0 }
            let statuses = self.state.statusFrames
            let statusOK = statuses.count == (card?.sections.compactMap { $0.extras.status }.count ?? 0)
                && statuses.values.allSatisfy { link in
                    statusClick = nil
                    return self.state.endPointer(at: CGPoint(x: link.frame.midX, y: link.frame.midY))
                        && statusClick == link.url
                }
            let ok = fonts && host.frame.height > 150 && tiles == (self.state.presenter?.tabs.count ?? -1)
                && self.menu.items.count == 7 && routeOK && policyFrames.count == targets.count * SpendLevel.allCases.count
                && providerAssetCount == ProviderLogo.allCases.count && dragOK && statusOK
            print("selftest \(ok ? "ok" : "FAILED"): fonts \(fonts), logos \(providerAssetCount), menu items \(self.menu.items.count), tiles \(tiles), dial stops \(policyFrames.count), drag \(dragOK), status links \(statuses.count), card height \(Int(host.frame.height))")
            exit(ok ? 0 : 1)
        }
    }

    private func selftestDrag() -> Bool {
        let original = state.presenter?.tabs.map(\.id) ?? []
        guard original.count >= 2 else { return false }
        let source = original[1], target = original[0]
        guard let src = state.tileFrames[source], let dst = state.tileFrames[target] else { return false }
        let a = CGPoint(x: src.midX, y: src.midY), b = CGPoint(x: dst.midX, y: dst.midY)
        let suite = "org.crossfeed.selftest." + UUID().uuidString
        guard let defaults = UserDefaults(suiteName: suite) else { return false }
        defer { defaults.removePersistentDomain(forName: suite) }
        let order = TabOrder(defaults: defaults)
        var clicked: String?
        state.onSelect = { clicked = $0 }
        state.onReorder = { from, to in _ = order.move(from, onto: to, among: original) }
        state.beginPointer(at: a)
        guard state.endPointer(at: a), clicked == source, order.ordered(original) == original else { return false }
        clicked = nil
        let outside = CGPoint(x: -20, y: -20)
        state.beginPointer(at: a)
        state.movePointer(to: outside)
        guard state.endPointer(at: outside), clicked == nil, order.ordered(original) == original else { return false }
        state.beginPointer(at: a)
        state.movePointer(to: b)
        let expected = [source, target] + original.dropFirst(2)
        guard state.endPointer(at: b), clicked == nil, order.ordered(original) == expected,
              let newDefaults = UserDefaults(suiteName: suite) else { return false }
        return TabOrder(defaults: newDefaults).ordered(original) == expected
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
