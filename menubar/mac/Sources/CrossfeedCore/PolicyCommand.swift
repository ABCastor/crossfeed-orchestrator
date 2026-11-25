import Foundation

public struct PolicyChange: Equatable, Hashable {
    public let pool: String
    public let level: SpendLevel

    public init(pool: String, level: SpendLevel) {
        self.pool = pool
        self.level = level
    }
}

/// One in-flight write. Refusal removes the tentative selection, revealing the unchanged reading.
public struct PolicyEdits {
    public private(set) var pending: PolicyChange?
    public private(set) var notices: [String: String] = [:]

    public init() {}

    public mutating func begin(_ change: PolicyChange, current: SpendLevel?) -> Bool {
        guard pending == nil, let current, current != change.level else { return false }
        pending = change
        notices[change.pool] = nil
        return true
    }

    public func selection(for pool: String, stored: SpendLevel?) -> SpendLevel? {
        pending?.pool == pool ? pending?.level : stored
    }

    public mutating func finish(_ result: Result<Void, PolicyRefusal>, readingAvailable: Bool) {
        guard let change = pending else { return }
        pending = nil
        switch result {
        case .success:
            notices[change.pool] = readingAvailable ? nil : "Saved · reload for attribution"
        case .failure(let refusal):
            notices[change.pool] = refusal.message
        }
    }
}

public enum PolicyRefusal: Error, Equatable {
    case missingPool, cannotStart, timedOut, rejected

    public var message: String {
        switch self {
        case .missingPool: return "Pool no longer exists · reload the view"
        case .cannotStart: return "Could not start fleetctl"
        case .timedOut: return "Save timed out · reload to confirm"
        case .rejected: return "Fleetctl refused the save · check the console"
        }
    }
}

/// The tray is an operator surface, just like the signed console. Only a menu action uses this
/// command; polling never writes. Fleetctl validates the pool and locks and audits the write.
public struct PolicyCommand {
    let python: String
    let fleetctl: String
    let overlay: String
    let stateDir: String

    public init(python: String, fleetctl: String, overlay: String, stateDir: String) {
        self.python = python
        self.fleetctl = fleetctl
        self.overlay = overlay
        self.stateDir = stateDir
    }

    public func save(_ change: PolicyChange, environment: [String: String]? = nil) -> Result<Void, PolicyRefusal> {
        let process = Process()
        let absolutePython = python.hasPrefix("/")
        process.executableURL = URL(fileURLWithPath: absolutePython ? python : "/usr/bin/env")
        var args = absolutePython ? [fleetctl] : [python, fleetctl]
        if !overlay.isEmpty { args += ["--overlay", overlay] }
        if !stateDir.isEmpty { args += ["--state-dir", stateDir] }
        args += ["level", change.pool, change.level.rawValue, "--owner", "--reason", "Crossfeed menu bar"]
        process.arguments = args
        process.environment = environment
        let errors = Pipe()
        process.standardError = errors
        process.standardOutput = FileHandle.nullDevice
        do { try process.run() } catch { return .failure(.cannotStart) }
        let watchdog = DispatchWorkItem { if process.isRunning { process.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + 8, execute: watchdog)
        // Drain the pipe, but retain only the leading diagnostic; never expose raw stderr in the menu.
        var prefix = Data()
        while true {
            let bytes = errors.fileHandleForReading.availableData
            if bytes.isEmpty { break }
            prefix.append(contentsOf: bytes.prefix(max(0, 256 - prefix.count)))
        }
        process.waitUntilExit()
        watchdog.cancel()
        if process.terminationReason == .uncaughtSignal { return .failure(.timedOut) }
        if process.terminationStatus == 0 { return .success(()) }
        let error = String(decoding: prefix, as: UTF8.self)
        return .failure(error.hasPrefix("fleetctl: unknown pool ") ? .missingPool : .rejected)
    }
}
