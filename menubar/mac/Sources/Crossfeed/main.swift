// Crossfeed menu bar app for macOS. It reads fleetctl glance and saves operator spend changes through
// fleetctl level: no provider readers, cookies, tokens or network calls live in this process.
//
// The menu is CodexBar's structure (MIT, Copyright (c) 2026 Peter Steinberger): a status item whose
// NSMenu holds one hosted SwiftUI card (provider tiles, usage bars, pace marker) above native action rows.
import AppKit
import CrossfeedCore
import CrossfeedUI
import SwiftUI
import UserNotifications

struct Config {
    var python = "python3"
    var fleetctl = ""
    var overlay = ""
    var state = ""
    var fixture = ""
    var fonts = ""
    /// panel.json for this run. A fixture or self test run never touches the real one.
    var panel = ""
    var selftest = false
    var selftestTab: String?
    /// With -selftest: drive a real drag through the host view's mouse events and save frames here.
    var dragFrames: String?
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
        case "panel": c.panel = next; i += 1
        case "selftest": c.selftest = true
        case "tab": c.selftestTab = next; i += 1
        case "dragframes": c.dragFrames = next; i += 1
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

enum ResetRunner {
    /// `fleetctl use-reset claude --key KEY --yes --json`, 30 s at most, one try. Its JSON is read; nothing else is shown.
    static func run(_ c: Config, key: String) -> ResetOutcome {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: c.python.hasPrefix("/") ? c.python : "/usr/bin/env")
        var args = c.python.hasPrefix("/") ? [] : [c.python]
        args.append(c.fleetctl)
        if !c.overlay.isEmpty { args += ["--overlay", c.overlay] }
        if !c.state.isEmpty { args += ["--state-dir", c.state] }
        args += ["use-reset", "claude", "--key", key, "--yes", "--json"]
        process.arguments = args
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return .failed("Could not start") }
        var timedOut = false
        let watchdog = DispatchWorkItem { if process.isRunning { timedOut = true; process.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + 30, execute: watchdog)
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        watchdog.cancel()
        if timedOut { return .failed("No answer") }
        return ResetOutcome.parse(data.prefix(1 << 16), exitCode: process.terminationStatus)
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

final class App: NSObject, NSApplicationDelegate, NSMenuDelegate, UNUserNotificationCenterDelegate {
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
    /// panel.json: the one place tab order, hidden tabs, tile order and Used/Left live.
    let panelStore: PanelPrefsStore
    var escapeMonitor: Any?
    /// The menu bar choices. A self test writes to a throwaway domain, never to the operator's preferences.
    let barStore: BarPrefsStore
    let console: ConsoleSession
    let alertStore: AlertStore
    var alertState: AlertState
    var gate = PaintGate()
    var repaintScheduled = false
    /// The one panel height of the next menu: tallest tab or settings, held to the screen. It changes only while the menu is closed.
    var panelHeight: CGFloat?

    init(config: Config) {
        self.config = config
        self.console = ConsoleSession(config: config)
        let isolated = config.selftest || !config.fixture.isEmpty
        self.panelStore = PanelPrefsStore(url: !config.panel.isEmpty ? URL(fileURLWithPath: config.panel)
            : (isolated ? FileManager.default.temporaryDirectory.appendingPathComponent("crossfeed-panel-\(UUID().uuidString).json")
                        : PanelPrefsStore.defaultURL()))
        self.barStore = BarPrefsStore(defaults: config.selftest
            ? (UserDefaults(suiteName: "org.crossfeed.selftest.bar." + UUID().uuidString) ?? .standard) : .standard)
        self.alertStore = AlertStore(defaults: .standard)
        self.alertState = config.selftest ? AlertState() : alertStore.load()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        state.onSelect = { [weak self] id in self?.select(id) }
        state.prefs = barStore.load()
        if !config.selftest && config.fixture.isEmpty { PanelMigration.run(store: panelStore, defaults: .standard) }
        state.panel = panelStore.load()
        state.onPanelChange = { [weak self] prefs in self?.panelStore.save(prefs) }
        state.onBlockDragActive = { [weak self] active in self?.listenForEscape(active) }
        state.onControl = { [weak self] control in self?.handle(control) }
        state.canEditPolicy = config.fixture.isEmpty || config.selftest
        state.onPolicyChange = { [weak self] change in self?.changePolicy(change) }
        state.onReorder = { [weak self] source, target in self?.reorder(source, onto: target) }
        state.onOpenConsole = { [weak self] in self?.openConsole() }
        state.onReload = { [weak self] in self?.reload() }
        state.onAbout = { [weak self] in self?.about() }
        state.onQuit = { NSApp.terminate(nil) }
        console.onFailure = { [weak self] failure in
            guard let self, self.state.actionFailure != failure else { return }
            self.state.actionFailure = failure
            self.remeasureHeights()
            self.syncMenu()
        }
        state.failure = "Reading quotas…"
        if config.selftest, let tab = config.selftestTab { state.selected = tab }
        menu.delegate = self
        menu.autoenablesItems = false
        if !config.selftest && Bundle.main.bundleIdentifier != nil {
            UNUserNotificationCenter.current().delegate = self
        }
        rebuildMenu()
        if config.selftest { selftest(); return }
        let status = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        // A stable name lets macOS (and bar managers) remember where the operator put the item across restarts.
        status.autosaveName = "org.crossfeed.tray"
        status.menu = menu
        item = status
        paintStatusItem()
        reload()
        scheduleReload()
        timers.append(Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in self?.tick() })
        // A display change, a new Space or waking from sleep can leave the item blank or off screen:
        // put it back, then paint it again.
        let center = NotificationCenter.default, workspace = NSWorkspace.shared.notificationCenter
        center.addObserver(self, selector: #selector(restoreStatusItem), name: NSApplication.didChangeScreenParametersNotification, object: nil)
        workspace.addObserver(self, selector: #selector(restoreStatusItem), name: NSWorkspace.activeSpaceDidChangeNotification, object: nil)
        workspace.addObserver(self, selector: #selector(restoreStatusItem), name: NSWorkspace.didWakeNotification, object: nil)
    }

    var reloadTimer: Timer?
    /// A reset was just used: the row says so while the menu is open, and the next reading is taken when it closes.
    var readAgainOnClose = false

    /// The reading interval is the operator's choice; the 20 s timer only moves countdowns.
    func scheduleReload() {
        reloadTimer?.invalidate()
        reloadTimer = Timer.scheduledTimer(withTimeInterval: TimeInterval(state.prefs.refreshSeconds), repeats: true) { [weak self] _ in self?.reload() }
    }

    @objc func restoreStatusItem() {
        guard let status = item else { return }
        status.isVisible = true
        status.length = NSStatusItem.variableLength
        gate.forget()
        paintStatusItem()
    }

    // MARK: Controls

    func handle(_ control: MenuControl) {
        var prefs = state.prefs
        switch control {
        case .refresh: reload(); return
        case .openTab(let id): select(id); return
        case .usage(let pool): menu.cancelTracking(); console.open(.usage(pool: pool)); return
        case .console: menu.cancelTracking(); console.open(.providers); return
        case .openURL(let url):
            guard url.scheme == "https" else { return }
            menu.cancelTracking()
            NSWorkspace.shared.open(url)
            return
        case .settingsPage(let page): withAnimation(MenuState.animation) { state.settingsPage = page; state.addOpen = false }; return
        case .toggleTab(let id): toggleTab(id); return
        case .moveTab(let id, let step): moveTab(id, by: step); return
        case .panelShow(let show): state.updatePanel { $0.show = show }; relayout(); return
        case .settingsTab(let id): state.settingsTab = id; state.addOpen = false; return
        case .toggleTile(let tab, let id):
            let on = state.panel.listed(tab, builtIns: PanelPrefs.defaultTiles(for: tab)).on.contains(id)
            state.updatePanel { $0.setTile(tab, id, on: !on) }
            relayout(); return
        case .moveTile(let tab, let id, let step):
            state.updatePanel { prefs in
                var ids = prefs.listed(tab, builtIns: PanelPrefs.defaultTiles(for: tab)).on
                guard let from = ids.firstIndex(of: id), ids.indices.contains(from + step) else { return }
                ids.swapAt(from, from + step)
                prefs.setTileOrder(tab, shown: ids)
            }
            return
        case .addOpen: state.addOpen.toggle(); relayout(); return
        case .addTile(let tab, let id):
            state.updatePanel { $0.setTile(tab, id, on: true) }
            state.addOpen = false
            relayout(); return
        case .policyOpen: state.policyOpen.toggle(); relayout(); return
        case .resetAsk(let key): state.resetFlow = ResetFlow(key: key, phase: .confirm); return
        case .resetCancel: state.resetFlow = nil; return
        case .resetUse(let key): useReset(key); return
        case .settings, .back:
            // Same panel height on both panes, so the menu does not move.
            withAnimation(MenuState.animation) { state.showsSettings = control == .settings; state.settingsPage = 0; state.addOpen = false }
            return
        case .reset: barStore.reset(); prefs = BarPrefs()
        case .show(let id): prefs.show = id
        case .window(let name): prefs.window = name
        case .count(let count): prefs.count = count
        case .icon(let icon): prefs.icon = icon
        case .text(let text): prefs.text = text
        case .extra(let extra): prefs.extra = extra
        case .resets(let reset): prefs.reset = reset
        case .refreshEvery(let seconds): prefs.refreshSeconds = seconds
        case .notifyLow: prefs.notifyLow.toggle()
        case .notifyReset: prefs.notifyReset.toggle()
        }
        if (control == .notifyLow && prefs.notifyLow) || (control == .notifyReset && prefs.notifyReset) { askForNotifications(control) }
        guard prefs != state.prefs else { return }
        state.prefs = prefs
        barStore.save(prefs)
        if prefs.refreshSeconds != reloadTimer.map({ Int($0.timeInterval) }) { scheduleReload() }
        paintStatusItem()
        remeasureHeights()
        syncMenu()
    }

    // MARK: Notifications

    /// Permission is asked when the operator turns a notification on, never before. If it is refused the toggle goes back off.
    func askForNotifications(_ control: MenuControl) {
        guard !config.selftest, Bundle.main.bundleIdentifier != nil else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { [weak self] granted, _ in
            guard !granted else { return }
            DispatchQueue.main.async {
                guard let self else { return }
                var prefs = self.state.prefs
                if control == .notifyLow { prefs.notifyLow = false } else { prefs.notifyReset = false }
                self.state.prefs = prefs
                self.barStore.save(prefs)
                self.state.actionFailure = "Notifications are off for Crossfeed in System Settings"
                self.remeasureHeights()
            }
        }
    }

    /// One new reading in: what to tell, once. Memory moves on even with both toggles off.
    func evaluateAlerts(_ presenter: Presenter) {
        guard config.fixture.isEmpty, !config.selftest else { return }
        let result = AlertEngine.step(alertState, AlertEngine.windows(of: presenter), now: presenter.now,
                                      low: state.prefs.notifyLow, reset: state.prefs.notifyReset)
        alertState = result.state
        alertStore.save(result.state)
        guard Bundle.main.bundleIdentifier != nil else { return }
        for event in result.events {
            let content = UNMutableNotificationContent()
            content.title = event.title
            content.body = event.body
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: event.id, content: content, trigger: nil))
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner])
    }

    // MARK: Data

    func reload() {
        guard !loading, state.edits.pending == nil else { return }
        loading = true
        if menuIsOpen { state.isReloading = true }
        let c = config, readingRevision = revision
        io.async { [weak self] in
            let result = Reader.load(c)
            RunLoop.main.perform(inModes: [.common, .eventTracking]) {
                guard let self else { return }
                self.loading = false
                self.state.isReloading = false
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
            let presenter = Presenter(glance: data, loadedAt: loadedAt, now: loadedAt, resetClock: state.prefs.reset == .clock)
            evaluateAlerts(presenter)
        case .failure:
            // Keep showing the last reading: its age grows in the header instead of vanishing.
            if glance == nil { state.failure = "Quotas unavailable" }
        }
    }

    /// Rebuilds the presenter against the clock so countdowns and ages move without a new reading.
    func tick() {
        if let glance {
            let presenter = Presenter(glance: glance, loadedAt: loadedAt, now: Date(), resetClock: state.prefs.reset == .clock)
            state.presenter = presenter
        } else {
            state.presenter = nil
        }
        paintStatusItem()
        if panelHeight == nil || !menuIsOpen { remeasureHeights() }
        syncMenu()
    }

    /// The tallest tab (or the settings pane), held to the screen. Measured from the reading, never while a menu is
    /// tracking: the height is applied when the menu is next built, so the panel never moves under the pointer.
    func remeasureHeights() {
        guard let presenter = state.presenter else { panelHeight = nil; return }
        let screen = item?.button?.window?.screen ?? NSScreen.main
        // Room for the menu bar, the three native rows under the card and a margin.
        let cap = max(360, (screen?.visibleFrame.height ?? 800) - 150)
        let measured = MainActor.assumeIsolated {
            PanelSizing.measure(presenter: presenter, prefs: state.prefs, panel: state.panel, failure: nil, cap: cap)
        }
        if menuIsOpen, panelHeight != nil { return }
        panelHeight = measured.panel
    }

    /// Rebuilds the menu only when its height should change, and never under an open menu that already has one.
    func syncMenu() {
        if menuIsOpen && host != nil && state.panelHeight != nil { return }
        if host == nil || (panelHeight ?? 0) != (state.panelHeight ?? 0) { rebuildMenu() }
    }

    func select(_ id: String) {
        guard id != state.current else { return }
        state.choose(id)
    }

    /// Show or hide a provider tab. Overview is not a provider and cannot be hidden.
    func toggleTab(_ id: String) {
        guard id != Presenter.overviewID else { return }
        state.updatePanel { $0.toggleTab(id) }
        relayout()
    }

    func moveTab(_ id: String, by step: Int) {
        guard !id.isEmpty, step != 0, let ids = state.presenter?.tabs.map(\.id) else { return }
        let order = state.panel.orderedTabs(ids).filter { $0 != "other" }
        guard let from = order.firstIndex(of: id), order.indices.contains(from + step) else { return }
        reorder(id, onto: order[from + step])
    }

    /// The height may change when tiles come and go; it is applied when the menu is next built.
    func relayout() {
        remeasureHeights()
        syncMenu()
    }

    /// Runs `fleetctl use-reset claude --key KEY --yes --json`: once, no retry, and says what it printed in a few words.
    func useReset(_ key: String) {
        guard state.resetFlow?.key == key, state.resetFlow?.phase == .confirm else { return }
        state.resetFlow = ResetFlow(key: key, phase: .running)
        // A fixture is not an account: nothing is sent, unless the self test is given its own stand-in for fleetctl.
        guard (config.fixture.isEmpty && !config.selftest) || (config.selftest && config.fleetctl.hasPrefix("/")) else {
            state.resetFlow = ResetFlow(key: key, phase: .failed("Fixture: nothing sent"))
            return
        }
        let c = config
        io.async { [weak self] in
            let outcome = ResetRunner.run(c, key: key)
            RunLoop.main.perform(inModes: [.common, .eventTracking]) {
                guard let self, self.state.resetFlow?.key == key else { return }
                switch outcome {
                case .used(let left):
                    self.state.resetFlow = ResetFlow(key: key, phase: .done(left))
                    if self.menuIsOpen { self.readAgainOnClose = true } else { self.reload() }
                case .failed(let why): self.state.resetFlow = ResetFlow(key: key, phase: .failed(why))
                }
            }
        }
    }

    /// Escape while a block is carried puts it back and writes nothing. A local monitor sees the key during menu
    /// tracking; it is installed only for the length of a drag.
    func listenForEscape(_ on: Bool) {
        if on, escapeMonitor == nil {
            escapeMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                guard event.keyCode == 53, let self, self.state.isCarryingBlock else { return event }
                self.state.cancelBlockDrag()
                return nil
            }
        } else if !on, let monitor = escapeMonitor {
            NSEvent.removeMonitor(monitor)
            escapeMonitor = nil
        }
    }

    func reorder(_ source: String, onto target: String) {
        guard let ids = state.presenter?.tabs.map(\.id) else { return }
        state.updatePanel { $0.moveTab(source, onto: target, among: ids) }
    }

    // MARK: Status item and menu

    func paintStatusItem() {
        guard let button = item?.button else { return }
        let summary = state.presenter?.barSummary(state.prefs)
            ?? BarSummary(text: "", left: nil, right: nil, deficit: false, tooltip: "Crossfeed · reading quotas")
        // Not while the menu is open or a click is landing, and not when nothing changed: repainting the item under
        // the pointer can swallow the click. A refused paint is asked again on the next tick and when the menu closes.
        let busy = menuIsOpen || NSEvent.pressedMouseButtons != 0
        guard gate.shouldPaint(summary, busy: busy) else {
            if busy && summary != gate.painted && !repaintScheduled {
                repaintScheduled = true
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                    self?.repaintScheduled = false
                    self?.paintStatusItem()
                }
            }
            return
        }
        let icon = MenuBarIcon.image(for: summary)
        button.image = icon
        button.imagePosition = icon == nil ? .noImage : .imageLeft
        // A one-colour logo takes the terracotta of a deficit; the tanks and colour marks carry their own.
        button.contentTintColor = icon?.isTemplate == true && summary.deficit ? CF.humanNS : nil
        var attrs: [NSAttributedString.Key: Any] = [.font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)]
        if summary.deficit { attrs[.foregroundColor] = CF.humanNS }
        let lead = icon == nil ? "" : " "
        button.attributedTitle = NSAttributedString(string: summary.text.isEmpty ? "" : lead + summary.text, attributes: attrs)
        // No hover tooltip: a tooltip window can outlive the item's repaint and stay on screen after the menu closes.
        // The sentence stays available to VoiceOver.
        button.toolTip = nil
        button.setAccessibilityLabel(summary.tooltip.replacingOccurrences(of: "\n", with: ". "))
        button.setAccessibilityRoleDescription("Crossfeed quota")
    }

    var menuIsOpen = false
    var host: MenuHostingView?

    func menuWillOpen(_ menu: NSMenu) {
        menuIsOpen = true
        // The cached reading is on screen already; read again only when it is older than 30 s, and update in place.
        if glance == nil || Date().timeIntervalSince(loadedAt) > 30 { reload() }
    }

    func menuDidClose(_ menu: NSMenu) {
        menuIsOpen = false
        state.showsSettings = false
        state.settingsPage = 0
        state.addOpen = false
        state.policyOpen = false
        if state.resetFlow?.phase != .running { state.resetFlow = nil }
        if readAgainOnClose { readAgainOnClose = false; reload() }
        // The menu opens on Overview every time.
        state.selected = Presenter.overviewID
        state.isReloading = false
        state.cancelPointer()
        paintStatusItem()
        remeasureHeights()
        rebuildMenu()
    }

    func rebuildMenu() {
        menu.removeAllItems()
        state.panelHeight = panelHeight
        let host = MenuHostingView(model: state)
        host.frame = NSRect(x: 0, y: 0, width: MenuMetrics.width, height: panelHeight ?? ceil(host.fittingSize.height))
        self.host = host
        let card = NSMenuItem()
        card.view = host
        menu.addItem(card)
        menu.addItem(.separator())
        menu.addItem(action("Open console", symbol: "arrow.up.right.square", #selector(openConsole)))
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
    @objc func about() { console.open(.about) }
    @objc func quit() { NSApp.terminate(nil) }

    func applicationWillTerminate(_ notification: Notification) { console.stop() }

    // MARK: Self test (builds the menu without a status item, shows nothing)

    /// Steps run one after another, each on its own turn of the run loop, so every view has laid out before the next click.
    func selftest() {
        guard case .success(let data) = Reader.load(config) else { print("selftest: cannot read the fixture"); exit(1) }
        glance = data
        tick()
        let host = MenuHostingView(model: state)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: MenuMetrics.width, height: 600), styleMask: .borderless,
                              backing: .buffered, defer: false)
        window.contentView = host
        host.frame = NSRect(x: 0, y: 0, width: MenuMetrics.width, height: ceil(host.fittingSize.height))
        state.selected = Presenter.overviewID
        var log: [String] = []
        var failed = false
        func check(_ name: String, _ ok: Bool) { if !ok { failed = true; log.append("FAIL " + name) } }
        func settle() {
            host.invalidateIntrinsicContentSize(); host.needsLayout = true
            host.frame = NSRect(x: 0, y: 0, width: MenuMetrics.width, height: ceil(host.fittingSize.height))
            host.layoutSubtreeIfNeeded(); window.displayIfNeeded(); host.display()
        }
        /// Click a routed control by its position, the way the host view does, and say what it routed to.
        func click(_ control: MenuControl, real: Bool = false) -> Bool {
            guard let rect = state.controlFrames[control] else { return false }
            var routed: MenuControl?
            let keep = state.onControl
            state.onControl = { routed = $0; if real { keep($0) } }
            defer { state.onControl = keep }
            return state.endPointer(at: CGPoint(x: rect.midX, y: rect.midY)) && routed == control
        }
        var steps: [() -> Void] = []
        func step(_ body: @escaping () -> Void) { steps.append(body) }
        let ids = state.presenter?.tabs.map(\.id) ?? []
        let providers = state.presenter?.overviewPage(order: ids.filter { $0 != "other" }, show: .used).providers.map(\.id) ?? []

        step { settle() }
        step { // Overview: every provider tile opens its tab; the links tile goes to the console
            check("overview tiles", !providers.isEmpty && providers.allSatisfy { click(.openTab($0)) })
            check("overview links", click(.usage(nil)) && click(.console))
            check("tab strip", self.state.tileFrames.count == ids.count + 1)
            check("provider tiles are draggable", self.state.blockFrames.keys.filter { $0.hasPrefix("provider:") }.count == providers.count)
            self.state.selected = self.config.selftestTab ?? "claude"
            settle()
        }
        step { settle() }
        step { // a provider tab: header, links, a reset flow, the policy popup
            let tab = self.state.current
            let page = self.state.presenter?.providerPage(for: tab, show: .used)
            check("header buttons", click(.refresh) && click(.settings))
            if let page {
                for link in page.links {
                    switch link.kind {
                    case .usage(let pool): check("usage link", click(.usage(pool)))
                    case .status(let url): check("status link", click(.openURL(url)))
                    case .console: check("console link", click(.console))
                    }
                }
                if let reset = page.resets.first(where: \.canUse) {
                    check("Use routes", click(.resetAsk(reset.id), real: true))
                    check("confirm is asked", self.state.resetFlow == ResetFlow(key: reset.id, phase: .confirm))
                }
            }
            settle()
        }
        step { settle() }
        step {
            if let flow = self.state.resetFlow {
                check("confirm has Cancel and Use", self.state.controlFrames[.resetCancel] != nil && self.state.controlFrames[.resetUse(flow.key)] != nil)
                check("Cancel closes it", click(.resetCancel, real: true) && self.state.resetFlow == nil)
            }
            if self.state.presenter?.providerPage(for: self.state.current, show: .used)?.policy != nil {
                check("policy popup", click(.policyOpen, real: true) && self.state.policyOpen)
            }
            settle()
        }
        step { settle() }
        step { // with a stand-in fleetctl: ask again, confirm, and the row reports what it printed
            guard self.config.fleetctl.hasPrefix("/"), let reset = self.state.presenter?.providerPage(for: self.state.current, show: .used)?.resets.first(where: \.canUse) else { return }
            check("asks again", click(.resetAsk(reset.id), real: true))
            settle()
        }
        step { settle() }
        step {
            guard self.config.fleetctl.hasPrefix("/"), let flow = self.state.resetFlow else { return }
            check("Use runs it", click(.resetUse(flow.key), real: true))
        }
        step { settle() }
        step {
            if self.config.fleetctl.hasPrefix("/") {
                check("reset reported: \(String(describing: self.state.resetFlow))", self.state.resetFlow?.phase == .done(0))
                self.state.resetFlow = nil
                check("a refusal is shown in plain words, no retry", ResetRunner.run(self.config, key: "claude-weekly-scoped-fable") == .failed("Anthropic did not accept the reset (HTTP 429)"))
            }
        }
        step { settle() }
        step {
            var hit: PolicyChange?
            self.state.onPolicyChange = { hit = $0 }
            let frames = self.state.policyFrames
            if self.state.policyOpen {
                check("dial stops", !frames.isEmpty && frames.allSatisfy { change, rect in
                    hit = nil
                    return self.state.endPointer(at: CGPoint(x: rect.midX, y: rect.midY)) && hit == change
                })
            }
            _ = click(.policyOpen, real: true)
            check("tab drag", self.selftestTabDrag())
            check("tile drag", self.selftestTileDrag(settle: settle))
        }
        step { settle() }
        step { // settings: Show, a tile off and on again, the tab list, the menu bar page
            self.state.showsSettings = true; self.state.settingsPage = 0; settle()
        }
        step { settle() }
        step {
            let before = self.state.panel
            check("settings frames", click(.panelShow(.left), real: true) && self.state.panel.show == .left)
            check("show saved", self.panelStore.load().show == .left)
            _ = click(.panelShow(.used), real: true)
            check("tab chips", self.state.shownTabs.allSatisfy { click(.settingsTab($0.id), real: true) })
            _ = click(.settingsTab("claude"), real: true)
            settle()
            let on = self.state.panel.listed("claude", builtIns: PanelPrefs.providerTiles).on
            if let first = on.first(where: { $0 != "links" }) {
                check("tile off", click(.toggleTile("claude", first), real: true) && !self.state.panel.listed("claude", builtIns: PanelPrefs.providerTiles).on.contains(first))
                check("tile off saved", self.panelStore.load().hiddenTiles["claude"]?.contains(first) == true)
                settle()
                check("add tile list", click(.addOpen, real: true) && self.state.addOpen)
                settle()
                check("add tile", click(.addTile("claude", first), real: true) && self.state.panel.listed("claude", builtIns: PanelPrefs.providerTiles).on.contains(first))
            } else { check("claude tiles listed", false) }
            _ = before
            settle()
        }
        step { settle() }
        step {
            check("tabs page", click(.settingsPage(1), real: true))
            settle()
        }
        step { settle() }
        step {
            let ids = self.state.orderedTabs.map(\.id)
            check("tab switches", !ids.isEmpty && ids.allSatisfy { self.state.controlFrames[.toggleTab($0)] != nil })
            if let first = ids.first {
                check("hide a tab", click(.toggleTab(first), real: true) && !self.state.panel.isTabVisible(first))
                check("hidden saved", self.panelStore.load().hiddenTabs.contains(first))
                _ = click(.toggleTab(first), real: true)
            }
            check("bar page", click(.settingsPage(2), real: true))
            settle()
        }
        step { settle() }
        step {
            var wanted: [MenuControl] = [.back, .reset, .settingsPage(0), .settingsPage(1)] + BarPrefs.windowChoices.map { .window($0) }
            wanted += [.notifyLow, .notifyReset, .show(BarPrefs.pressed)] + (self.state.presenter?.barChoices.map { .show($0.id) } ?? [])
            wanted += BarPrefs.Count.allCases.map { .count($0) } + BarPrefs.Icon.allCases.map { .icon($0) }
            wanted += BarPrefs.Text.allCases.map { .text($0) } + BarPrefs.Extra.allCases.map { .extra($0) } + BarPrefs.Reset.allCases.map { .resets($0) } + BarPrefs.refreshChoices.map { .refreshEvery($0) }
            let missing = wanted.filter { self.state.controlFrames[$0] == nil }
            check("bar controls \(missing.prefix(3))", missing.isEmpty)
            for control in [MenuControl.show("codex"), .window("Week"), .count(.left), .icon(.logo), .text(.window), .notifyLow, .notifyReset, .extra(.reset), .resets(.clock), .refreshEvery(300)] {
                _ = click(control, real: true)
            }
            var expected = BarPrefs()
            expected.show = "codex"; expected.window = "Week"; expected.count = .left; expected.icon = .logo
            expected.text = .window; expected.notifyLow = true; expected.notifyReset = true; expected.extra = .reset; expected.reset = .clock; expected.refreshSeconds = 300
            check("bar prefs saved", self.state.prefs == expected && self.barStore.load() == expected)
            _ = click(.reset, real: true)
            check("bar prefs reset", self.state.prefs == BarPrefs() && self.barStore.load() == BarPrefs())
            check("back", click(.back, real: true) && !self.state.showsSettings)
            let fonts = NSFont.systemFont(ofSize: 13).isFixedPitch == false
            let ok = !failed && fonts && self.menu.items.count == 6 && providerAssetCount == ProviderLogo.allCases.count
            print("selftest \(ok ? "ok" : "FAILED"): logos \(providerAssetCount), menu items \(self.menu.items.count), tabs \(ids.count), providers \(providers.count), panel file \(self.panelStore.exists)" + (log.isEmpty ? "" : "\n" + log.joined(separator: "\n")))
            try? FileManager.default.removeItem(at: self.panelStore.url)
            exit(ok ? 0 : 1)
        }
        if let dir = config.dragFrames {
            self.state.selected = self.config.selftestTab ?? "claude"
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                settle()
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { self.recordDrag(host: host, window: window, into: dir) }
            }
            return
        }
        func run(_ index: Int) {
            guard index < steps.count else { return }
            steps[index]()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { run(index + 1) }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { run(0) }
    }

    /// Tab strip: a press and release selects, a press that travels past the slop and lands on another tab reorders,
    /// a drag that ends outside does nothing. All through the pointer methods the host's mouse events call.
    private func selftestTabDrag() -> Bool {
        let original = state.presenter?.tabs.map(\.id) ?? []
        guard original.count >= 2 else { return false }
        let source = original[1], target = original[0]
        guard let src = state.tileFrames[source], let dst = state.tileFrames[target] else { return false }
        let a = CGPoint(x: src.midX, y: src.midY), b = CGPoint(x: dst.midX, y: dst.midY)
        let saved = state.panel
        defer { state.panel = saved }
        var clicked: String?
        state.onSelect = { clicked = $0 }
        let reorder = state.onReorder
        state.onReorder = { from, to in self.state.panel.moveTab(from, onto: to, among: original) }
        defer { state.onReorder = reorder }
        state.beginPointer(at: a)
        guard state.endPointer(at: a), clicked == source, state.panel.orderedTabs(original) == original else { return false }
        clicked = nil
        let outside = CGPoint(x: -20, y: -20)
        state.beginPointer(at: a)
        state.movePointer(to: outside)
        guard state.endPointer(at: outside), clicked == nil, state.panel.orderedTabs(original) == original else { return false }
        state.beginPointer(at: a)
        state.movePointer(to: b)
        let expected = [source, target] + original.dropFirst(2)
        return state.endPointer(at: b) && clicked == nil && state.panel.orderedTabs(original) == expected
    }

    /// Tiles: a press that travels past the slop carries the tile; released past the next tile it takes that place, saved to panel.json.
    private func selftestTileDrag(settle: () -> Void) -> Bool {
        let tab = state.current
        let ids = state.blockFrames.filter { $0.value.tab == tab }.sorted { $0.value.index < $1.value.index }.map(\.key)
        guard ids.count >= 2, let first = state.blockFrames[ids[0]]?.rect, let second = state.blockFrames[ids[1]]?.rect else { return false }
        let press = CGPoint(x: first.minX + 20, y: first.minY + 6)
        var changed: (String, [String])?
        let keep = state.onReorderBlocks
        state.onReorderBlocks = { changed = ($0, $1); keep($0, $1) }
        defer { state.onReorderBlocks = keep }
        state.beginPointer(at: press)
        state.movePointer(to: CGPoint(x: press.x, y: press.y + 3))
        let notYet = !state.isCarryingBlock
        state.movePointer(to: CGPoint(x: press.x, y: press.y + 10))
        let carried = state.isCarryingBlock
        let travel = second.maxY - first.minY
        state.movePointer(to: CGPoint(x: press.x, y: press.y + travel))
        state.cancelPointer()   // a cancelled drag writes nothing
        let cancelled = changed == nil
        return notYet && carried && cancelled
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
