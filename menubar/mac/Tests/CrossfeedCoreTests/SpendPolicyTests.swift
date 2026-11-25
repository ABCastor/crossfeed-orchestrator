import Foundation
import XCTest
@testable import CrossfeedCore

final class SpendPolicyTests: XCTestCase {
    func testMenuWritesThroughFleetctlAndRestoresSelectionOnRefusal() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let work = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: work) }
        let overlay = work.appendingPathComponent("overlay.json")
        try FileManager.default.copyItem(at: root.appendingPathComponent("examples/access-overlay.example.json"), to: overlay)
        let state = work.appendingPathComponent("state")
        let runtime = state.appendingPathComponent("runtime.json")
        let script = root.appendingPathComponent("scripts/fleetctl.py").path
        let env = ["HOME": work.path, "PATH": ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin",
                   "FLEET_NO_AUTO_REFRESH": "1", "PYTHONDONTWRITEBYTECODE": "1"]
        let command = PolicyCommand(python: "python3", fleetctl: script, overlay: overlay.path, stateDir: state.path)
        var menu = PolicyEdits()

        let click = PolicyChange(pool: "claude", level: .low)
        XCTAssertTrue(menu.begin(click, current: .normal))
        let accepted = command.save(click, environment: env)
        try accepted.get()
        menu.finish(accepted, readingAvailable: true)
        XCTAssertNil(menu.pending)
        XCTAssertTrue(menu.notices.isEmpty)

        let saved = try Data(contentsOf: runtime)
        let record = try XCTUnwrap(JSONSerialization.jsonObject(with: saved) as? [String: Any])
        let history = try XCTUnwrap(record["control_history"] as? [[String: String]])
        XCTAssertEqual(history.count, 1)
        XCTAssertEqual(history.first?["key"], "level:claude")
        XCTAssertEqual(history.first?["before"], "normal")
        XCTAssertEqual(history.first?["value"], "low")
        XCTAssertEqual(history.first?["actor"], "owner")
        XCTAssertEqual(history.first?["reason"], "Crossfeed menu bar")
        XCTAssertNotNil(history.first?["when"])
        XCTAssertEqual((record["switches"] as? [String: String])?["claude"], "low")
        XCTAssertEqual((record["control_limits"] as? [String: [String: String]])?["level:claude"]?["value"], "low")

        // A pool disappeared from the roster after the menu read it.
        let rejected = PolicyChange(pool: "missing-pool", level: .forced)
        XCTAssertTrue(menu.begin(rejected, current: .low))
        XCTAssertEqual(menu.selection(for: rejected.pool, stored: .low), .forced)
        XCTAssertFalse(menu.begin(click, current: .normal), "only one write may be in flight")
        let refused = command.save(rejected, environment: env)
        XCTAssertThrowsError(try refused.get()) { XCTAssertEqual($0 as? PolicyRefusal, .missingPool) }
        menu.finish(refused, readingAvailable: true)
        XCTAssertEqual(menu.selection(for: rejected.pool, stored: .low), .low)
        XCTAssertEqual(menu.notices[rejected.pool], "Pool no longer exists · reload the view")
        XCTAssertEqual(try Data(contentsOf: runtime), saved, "a refusal leaves the runtime and audit untouched")

        // The app reads the same audit record back via glance, not by guessing who moved it.
        let glance = Process()
        glance.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        glance.arguments = ["python3", script, "--overlay", overlay.path, "--state-dir", state.path,
                            "glance", "--json", "--no-refresh"]
        glance.environment = env
        let output = Pipe()
        glance.standardOutput = output
        glance.standardError = FileHandle.nullDevice
        try glance.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        glance.waitUntilExit()
        XCTAssertEqual(glance.terminationStatus, 0)
        let reading = try Glance.decode(data)
        let claude = try XCTUnwrap(reading.pools.first { $0.pool == "claude" })
        XCTAssertEqual(claude.level, "low")
        XCTAssertEqual(claude.levelControl?.actor, .operator_)
        XCTAssertEqual(claude.levelControl?.when, history.first?["when"])
        XCTAssertFalse(claude.models.isEmpty)
        XCTAssertFalse(String(decoding: data, as: UTF8.self).contains("Crossfeed menu bar"), "no free-form audit text in glance")
    }

    func testAttributionUsesOperatorAndAgentLabels() throws {
        let now = try XCTUnwrap(ISO8601DateFormatter().date(from: "2026-11-25T10:53:57Z"))
        let when = "2026-11-25T10:51:57Z"
        let agent = try JSONDecoder().decode(PolicyAttribution.self, from: Data(#"{"actor":"agent","when":"2026-11-25T10:51:57Z"}"#.utf8))
        let operator_ = try JSONDecoder().decode(PolicyAttribution.self, from: Data("{\"actor\":\"owner\",\"when\":\"\(when)\"}".utf8))
        XCTAssertEqual(agent.line(now: now), "Set by an agent · 2 min ago")
        XCTAssertEqual(operator_.line(now: now), "Set by you · 2 min ago")
    }
}
