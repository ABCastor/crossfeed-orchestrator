// Crossfeed menu: reordering the blocks of a provider card by drag, the "Declarative Dragging" way.
// A drag never moves a block to a free pixel. Every legal order is a state (the dragged block in slot 0,
// 1, 2 ...), each state has a layout, and the frame drawn is a blend of the two states nearest the pointer.
// Release settles on the nearest state; cancel settles on the start. Pure geometry: no UI in here.
import Foundation
import CoreGraphics

public enum BlockMotion {
    /// The gap between blocks, the one the rows of a card already use.
    public static let gap: CGFloat = 14
    /// Release, cancel and a keyboard move settle on this curve: 200 ms, cubic-bezier(.2, .8, .2, 1).
    public static let settle: Double = 0.2
    /// Reduced motion keeps the same path and shortens it (DRAG.md B).
    public static let settleReduced: Double = 0.15
    /// A mouse press becomes a drag after this much travel, in points (the family's slop).
    public static let slop: CGFloat = 7

    /// Top of each block, by block index, when the blocks stand in `order` (order[slot] = block index).
    public static func tops(order: [Int], heights: [CGFloat], gap: CGFloat = gap) -> [CGFloat] {
        var tops = [CGFloat](repeating: 0, count: heights.count)
        var y: CGFloat = 0
        for index in order where heights.indices.contains(index) {
            tops[index] = y
            y += heights[index] + gap
        }
        return tops
    }

    /// `order` with `moving` taken out and put into `slot`.
    public static func order(_ order: [Int], moving: Int, to slot: Int) -> [Int] {
        var result = order.filter { $0 != moving }
        result.insert(moving, at: max(0, min(slot, result.count)))
        return result
    }

    public struct Frame: Equatable {
        public var tops: [CGFloat]
        /// The slot the dragged block would take if released now.
        public var nearest: Int
        /// Where the dragged block would settle, in that slot.
        public var nearestTop: CGFloat
    }

    /// The frame drawn with the dragged block's top at `top`: it follows the pointer 1:1 along one axis (clamped to
    /// the first and last legal slot), and every other block sits at the blend of the two states either side of it.
    public static func frame(order: [Int], dragged: Int, top: CGFloat, heights: [CGFloat], gap: CGFloat = gap) -> Frame {
        let count = order.count
        guard count > 0, order.contains(dragged), heights.count >= count else {
            return Frame(tops: tops(order: order, heights: heights, gap: gap), nearest: 0, nearestTop: 0)
        }
        let states = (0..<count).map { tops(order: self.order(order, moving: dragged, to: $0), heights: heights, gap: gap) }
        let stops = states.map { $0[dragged] }
        let clamped = min(max(top, stops[0]), stops[count - 1])
        var k = 0
        guard count > 1 else { return Frame(tops: states[0], nearest: 0, nearestTop: stops[0]) }
        while k + 1 < count - 1, stops[k + 1] <= clamped { k += 1 }
        let span = stops[k + 1] - stops[k]
        let t = span > 0 ? (clamped - stops[k]) / span : 0
        var blended = (0..<heights.count).map { states[k][$0] + (states[k + 1][$0] - states[k][$0]) * t }
        blended[dragged] = clamped
        let nearest = t < 0.5 ? k : k + 1
        return Frame(tops: blended, nearest: nearest, nearestTop: stops[nearest])
    }

    /// Top of the block `index` in slot `slot`, with the others in `order` otherwise.
    public static func stop(order: [Int], dragged: Int, slot: Int, heights: [CGFloat], gap: CGFloat = gap) -> CGFloat {
        tops(order: self.order(order, moving: dragged, to: slot), heights: heights, gap: gap)[dragged]
    }
}

/// The order of one tab's blocks. Only order is saved; which blocks exist comes from the latest reading.
public struct SectionOrderStore {
    private let defaults: UserDefaults

    public init(defaults: UserDefaults) { self.defaults = defaults }

    public static func key(_ tab: String) -> String { "sectionOrder." + tab }

    public func saved(_ tab: String) -> [String] {
        (defaults.stringArray(forKey: Self.key(tab)) ?? []).filter { !$0.isEmpty && $0.count <= 80 }.prefix(32).map { $0 }
    }

    /// Keeps the slots of blocks that are absent from this reading, after the shown ones.
    public func save(_ tab: String, order: [String]) {
        let absent = saved(tab).filter { !order.contains($0) }
        defaults.set(order + absent, forKey: Self.key(tab))
    }

    public func reset(_ tab: String) { defaults.removeObject(forKey: Self.key(tab)) }

    /// The saved order over what is shown: saved ids that still exist first, then the new ones in their own order.
    public static func merged(saved: [String], available: [String]) -> [String] {
        let present = Set(available)
        var seen = Set<String>()
        return (saved.filter { present.contains($0) } + available).filter { seen.insert($0).inserted }
    }
}
