import AppKit
import CrossfeedCore

/// Starts at most one console for the tray. Its signed startup URL stays in memory and goes only
/// to Launch Services. Closing the tray stops only the process it started.
final class ConsoleSession {
    private let config: Config
    private var process: Process?
    private var output: FileHandle?
    private var startup = Data()
    private var signedURL: URL?
    private var pending: ConsolePage?
    var onFailure: (String?) -> Void = { _ in }

    init(config: Config) { self.config = config }

    func open(_ page: ConsolePage) {
        guard config.fixture.isEmpty, !config.selftest else { return }
        onFailure(nil)
        pending = page
        if process?.isRunning == true { openPending(); return }
        stop()
        pending = page

        let child = Process()
        let absolutePython = config.python.hasPrefix("/")
        child.executableURL = URL(fileURLWithPath: absolutePython ? config.python : "/usr/bin/env")
        var args = absolutePython ? [config.fleetctl] : [config.python, config.fleetctl]
        if !config.overlay.isEmpty { args += ["--overlay", config.overlay] }
        if !config.state.isEmpty { args += ["--state-dir", config.state] }
        child.arguments = args + ["console", "--no-open"]
        let pipe = Pipe()
        child.standardInput = FileHandle.nullDevice
        child.standardOutput = pipe
        child.standardError = FileHandle.nullDevice
        let handle = pipe.fileHandleForReading
        process = child
        output = handle
        handle.readabilityHandler = { [weak self, weak child] reader in
            let bytes = reader.availableData
            if bytes.isEmpty { reader.readabilityHandler = nil }
            RunLoop.main.perform(inModes: [.common, .eventTracking]) {
                guard let self, let child, self.process === child else { return }
                self.receive(bytes)
            }
        }
        child.terminationHandler = { [weak self] child in
            RunLoop.main.perform(inModes: [.common, .eventTracking]) {
                guard let self, self.process === child else { return }
                if self.pending != nil { self.onFailure("Could not open the console · try again") }
                self.stop()
            }
        }
        do {
            try child.run()
        } catch {
            fail()
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { [weak self, weak child] in
            guard let self, let child, self.process === child, self.signedURL == nil else { return }
            self.fail()
        }
    }

    private func receive(_ bytes: Data) {
        guard signedURL == nil else { return }
        guard startup.count + bytes.count <= 4096 else { fail(); return }
        startup.append(bytes)
        while let newline = startup.firstIndex(of: 10) {
            let line = String(decoding: startup[..<newline], as: UTF8.self)
            startup.removeSubrange(...newline)
            if let url = ConsolePage.signedURL(from: line) {
                signedURL = url
                startup.removeAll()
                openPending()
                return
            }
        }
        if bytes.isEmpty { fail() }
    }

    private func openPending() {
        guard let page = pending, let signedURL, let url = page.url(from: signedURL) else { return }
        pending = nil
        if !NSWorkspace.shared.open(url) { onFailure("Could not open the browser · try again") }
    }

    private func fail() {
        stop()
        onFailure("Could not open the console · try again")
    }

    func stop() {
        output?.readabilityHandler = nil
        output = nil
        if let process, process.isRunning { process.terminate() }
        process = nil
        pending = nil
        signedURL = nil
        startup.removeAll()
    }
}
