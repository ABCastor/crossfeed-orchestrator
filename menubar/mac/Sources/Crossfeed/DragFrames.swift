// -selftest -dragframes DIR: a real drag of the Claude tab's tiles, entirely offscreen. NSEvents (mouse down,
// dragged, up, Escape) are built against the borderless selftest window and delivered to the same
// MenuHostingView methods an open NSMenu calls; frames of the host view are saved as PNGs. No pointer is used.
import AppKit
import CrossfeedCore
import CrossfeedUI
import SwiftUI

extension App {
    func recordDrag(host: MenuHostingView, window: NSWindow, into dir: String) {
        let out = URL(fileURLWithPath: dir)
        try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        var log: [String] = []
        var failed = false
        func check(_ name: String, _ ok: Bool) { log.append("\(ok ? "ok  " : "FAIL") \(name)"); if !ok { failed = true } }
        state.onBlockDragActive = { [weak self] active in self?.listenForEscape(active) }
        var eventNumber = 0
        func event(_ type: NSEvent.EventType, y: CGFloat, x: CGFloat = 60) -> NSEvent {
            eventNumber += 1
            // The host is flipped; window coordinates are not.
            let location = NSPoint(x: x, y: host.bounds.height - y)
            return NSEvent.mouseEvent(with: type, location: location, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                      windowNumber: window.windowNumber, context: nil, eventNumber: eventNumber, clickCount: 1, pressure: 1)!
        }
        var frameNumber = 0
        func snap(_ name: String) {
            host.layoutSubtreeIfNeeded(); window.displayIfNeeded(); host.display()
            guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return }
            host.cacheDisplay(in: host.bounds, to: rep)
            frameNumber += 1
            let file = out.appendingPathComponent(String(format: "frame-%02d-%@.png", frameNumber, name))
            try? rep.representation(using: .png, properties: [:])?.write(to: file)
        }
        func ids() -> [String] {
            state.blockFrames.sorted { ($0.value.rect.minY.rounded(), $0.value.rect.minX) < ($1.value.rect.minY.rounded(), $1.value.rect.minX) }.map(\.key)
        }
        func rect(_ id: String) -> CGRect { state.blockFrames[id]?.rect ?? .zero }

        let start = ids()
        log.append("blocks top to bottom: \(start)")
        guard start.count >= 3 else { print("dragframes: need 3+ blocks on this tab, found \(start)"); exit(1) }
        // Grab the Resets tile and carry it down past the Tokens and Spend policy pair.
        let grabbed = start.contains("resets") ? "resets" : start[1], over = start[start.count - 1]
        let gRect = rect(grabbed), oRect = rect(over)
        let pressY = gRect.minY + 8, x: CGFloat = gRect.minX + 40
        let travel = oRect.minY - gRect.minY   // down to where the last tile stands
        var steps: [() -> Void] = []
        func later(_ block: @escaping () -> Void) { steps.append(block) }

        later { snap("rest") }
        later {
            host.mouseDown(with: event(.leftMouseDown, y: pressY, x: x))
            host.mouseDragged(with: event(.leftMouseDragged, y: pressY + 3, x: x))
            check("below slop: not a drag yet", !self.state.isCarryingBlock)
            host.mouseDragged(with: event(.leftMouseDragged, y: pressY + 9, x: x))
            check("past slop: block carried", self.state.isCarryingBlock)
        }
        later { snap("grabbed-lifted") }
        later { host.mouseDragged(with: event(.leftMouseDragged, y: pressY + travel * 0.35, x: x)) }
        later { snap("dragging-a-third") }
        later { host.mouseDragged(with: event(.leftMouseDragged, y: pressY + travel * 0.75, x: x)) }
        later { snap("dragging-neighbour-slid-up") }
        later {
            // Release well past the pair: the nearest legal order is further down.
            host.mouseUp(with: event(.leftMouseUp, y: pressY + travel * 0.75, x: x))
            check("released: no longer held", !self.state.isCarryingBlock)
        }
        later { snap("released-settling") }
        later { /* settle runs 200 ms */ }
        later { /* commit lands */ }
        later {
            snap("settled")
            let now = ids()
            check("order after release: \(grabbed) moved down \(now)", (now.firstIndex(of: grabbed) ?? 0) > (start.firstIndex(of: grabbed) ?? 0))
            let saved = self.panelStore.load().tiles["claude"] ?? []
            check("saved to panel.json tiles.claude: \(saved)", saved.firstIndex(of: grabbed) == now.firstIndex(of: grabbed))
        }
        // Cancel: pick the last block up, drag it to the top, press Escape.
        var before: [String] = []
        later {
            before = ids()
            let last = before[before.count - 1]
            let r = rect(last)
            host.mouseDown(with: event(.leftMouseDown, y: r.minY + 8, x: x))
            host.mouseDragged(with: event(.leftMouseDragged, y: r.minY - 80, x: x))
            check("cancel drag: carried", self.state.isCarryingBlock)
        }
        later { snap("cancel-carried-to-top") }
        later {
            let escape = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                          windowNumber: window.windowNumber, context: nil, characters: "\u{1b}", charactersIgnoringModifiers: "\u{1b}",
                                          isARepeat: false, keyCode: 53)!
            NSApp.sendEvent(escape)   // goes through NSEvent local monitors, as in menu tracking
            check("Escape: no longer carried", !self.state.isCarryingBlock)
        }
        later { /* settle back */ }
        later { /* settle back */ }
        later {
            snap("cancel-settled-back")
            check("Escape: order unchanged \(ids())", ids() == before)
            host.mouseDragged(with: event(.leftMouseDragged, y: 0, x: x))   // the press goes on after Escape: nothing follows
            host.mouseUp(with: event(.leftMouseUp, y: 0, x: x))
            check("moves and release after Escape do not take hold again", ids() == before && !self.state.isCarryingBlock)
        }
        later { /* any restarted drag would settle and commit by now */ }
        later { check("still unchanged after the settle window: \(ids())", ids() == before) }
        // Keyboard path: Move up on the last block.
        later {
            let last = before[before.count - 1]
            check("Move down disabled on the last block", !self.state.canMoveBlock(last, by: 1))
            check("Move up enabled", self.state.canMoveBlock(last, by: -1))
            self.state.moveBlock(last, by: -1)
        }
        later { /* settle */ }
        later { /* settle */ }
        later { /* commit */ }
        later {
            let now = ids()
            let moved = before[before.count - 1]
            check("Move up: the last tile is higher \(now)", (now.firstIndex(of: moved) ?? 99) < before.count - 1)
            check("persisted too", (self.panelStore.load().tiles["claude"] ?? []).firstIndex(of: moved) == now.firstIndex(of: moved))
            snap("keyboard-move-up")
            try? FileManager.default.removeItem(at: self.panelStore.url)
            print(log.joined(separator: "\n"))
            print("dragframes \(failed ? "FAILED" : "ok"): \(frameNumber) frames in \(dir)")
            exit(failed ? 1 : 0)
        }
        func run(_ index: Int) {
            guard index < steps.count else { return }
            steps[index]()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { run(index + 1) }
        }
        run(0)
    }
}
