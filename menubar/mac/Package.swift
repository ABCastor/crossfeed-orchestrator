// swift-tools-version:5.9
import PackageDescription

// No third-party dependencies: Foundation, AppKit and SwiftUI only.
let package = Package(
    name: "Crossfeed",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "Crossfeed", targets: ["Crossfeed"])],
    targets: [
        // Schema 1 of `fleetctl glance --json` and everything derived from it. Foundation only.
        .target(name: "CrossfeedCore"),
        // The menu's SwiftUI views, ported from CodexBar (MIT) and restyled. Also drives the snapshot tool.
        .target(name: "CrossfeedUI", dependencies: ["CrossfeedCore"]),
        .executableTarget(name: "Crossfeed", dependencies: ["CrossfeedCore", "CrossfeedUI"]),
        // Renders the open menu offscreen (NSHostingView) so no pointer, focus or screen is touched.
        .executableTarget(name: "CrossfeedSnapshot", dependencies: ["CrossfeedCore", "CrossfeedUI"]),
        .testTarget(name: "CrossfeedCoreTests", dependencies: ["CrossfeedCore"]),
    ]
)
