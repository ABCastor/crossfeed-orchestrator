// Crossfeed menu: dragging tiles, B+ style. The same "Declarative Dragging" rule as BlockMotion, now in two
// dimensions: tiles stand in rows, two half tiles side by side where they are neighbours, and a half tile alone
// spans the row. Every legal order is a state (the carried tile in slot 0, 1, 2 ...). The frame drawn follows the
// pointer: the carried tile rides under it, every other tile sits at the blend of the states weighted by how near
// the carried tile's place in each state is to the pointer (1 / distance^4, as the design's d2.js does).
// Release settles on the nearest state, cancel on the start. Pure geometry: no UI in here.
import Foundation
import CoreGraphics

public struct TileBox: Equatable {
    public var x: CGFloat, y: CGFloat, w: CGFloat, h: CGFloat
    public init(x: CGFloat, y: CGFloat, w: CGFloat, h: CGFloat) { self.x = x; self.y = y; self.w = w; self.h = h }
    public var origin: CGPoint { CGPoint(x: x, y: y) }
}

/// What the layout needs to know about each tile, by block index.
public struct TileMetrics: Equatable {
    /// A half tile sits beside another half tile when they are neighbours in the order.
    public var half: [Bool]
    /// Height at the full width (also a half tile alone in its row).
    public var full: [CGFloat]
    /// Height at half width; only read for half tiles.
    public var halfHeight: [CGFloat]
    public var width: CGFloat
    public var gap: CGFloat

    public init(half: [Bool], full: [CGFloat], halfHeight: [CGFloat], width: CGFloat, gap: CGFloat = TileMotion.gap) {
        self.half = half; self.full = full; self.halfHeight = halfHeight; self.width = width; self.gap = gap
    }

    public var halfWidth: CGFloat { (width - gap) / 2 }
}

public enum TileMotion {
    /// The gap between tiles, the design's 6.
    public static let gap: CGFloat = 6

    /// Where every tile sits when the tiles stand in `order` (order[slot] = block index).
    public static func view(order: [Int], metrics m: TileMetrics) -> [TileBox] {
        var boxes = [TileBox](repeating: TileBox(x: 0, y: 0, w: m.width, h: 0), count: m.full.count)
        var y: CGFloat = 0
        var i = 0
        while i < order.count {
            let a = order[i]
            guard boxes.indices.contains(a) else { i += 1; continue }
            if m.half[a], i + 1 < order.count, boxes.indices.contains(order[i + 1]), m.half[order[i + 1]] {
                let b = order[i + 1]
                let h = max(m.halfHeight[a], m.halfHeight[b])
                boxes[a] = TileBox(x: 0, y: y, w: m.halfWidth, h: h)
                boxes[b] = TileBox(x: m.halfWidth + m.gap, y: y, w: m.halfWidth, h: h)
                y += h + m.gap
                i += 2
            } else {
                boxes[a] = TileBox(x: 0, y: y, w: m.width, h: m.full[a])
                y += m.full[a] + m.gap
                i += 1
            }
        }
        return boxes
    }

    /// The height the tiles take in `order`.
    public static func height(order: [Int], metrics: TileMetrics) -> CGFloat {
        view(order: order, metrics: metrics).enumerated().map { $0.element.y + $0.element.h }.max() ?? 0
    }

    public static func order(_ order: [Int], moving: Int, to slot: Int) -> [Int] {
        BlockMotion.order(order, moving: moving, to: slot)
    }

    /// The slots the carried tile may take. `group[i]` names a set of tiles that must stay together (Overview's providers):
    /// a member moves inside its group, anything else moves around the whole group. Nil: every slot is legal.
    public static func legalSlots(order: [Int], dragged: Int, group: [Int?]?) -> [Int] {
        let all = Array(order.indices)
        guard let group, group.indices.contains(dragged) else { return all }
        return all.filter { slot in
            let next = Self.order(order, moving: dragged, to: slot)
            var closed = Set<Int>(), current: Int?
            for block in next {
                let g = group.indices.contains(block) ? group[block] : nil
                if g != current {
                    if let current { closed.insert(current) }
                    if let g, closed.contains(g) { return false }
                    current = g
                }
            }
            return true
        }
    }

    public struct Frame: Equatable {
        public var boxes: [TileBox]
        /// The slot the carried tile takes if released now.
        public var nearest: Int
        /// Where the carried tile's top left stands in that slot.
        public var nearestAnchor: CGPoint
    }

    private static func lerp(_ a: CGFloat, _ b: CGFloat, _ t: CGFloat) -> CGFloat { a + (b - a) * t }

    /// The frame with the carried tile's top left at `anchor` (the pointer's travel added to where it was lifted).
    public static func frame(order: [Int], dragged: Int, anchor: CGPoint, metrics: TileMetrics, legal: [Int]? = nil) -> Frame {
        let slots = legal ?? Array(order.indices)
        guard order.contains(dragged), !slots.isEmpty else {
            return Frame(boxes: view(order: order, metrics: metrics), nearest: 0, nearestAnchor: .zero)
        }
        let states = slots.map { view(order: Self.order(order, moving: dragged, to: $0), metrics: metrics) }
        let distances = states.map { hypot($0[dragged].x - anchor.x, $0[dragged].y - anchor.y) }
        let ni = distances.indices.min { distances[$0] < distances[$1] } ?? 0
        let near = states[ni]
        var boxes: [TileBox]
        if distances[ni] < 1e-3 || states.count == 1 {
            boxes = near
        } else {
            let weights = distances.map { 1 / pow($0, 4) }
            let top = weights[ni]
            let keep = weights.indices.filter { weights[$0] > top * 0.01 }
            let sum = keep.reduce(0) { $0 + weights[$1] }
            boxes = near
            for block in boxes.indices {
                var box = TileBox(x: 0, y: 0, w: 0, h: 0)
                for k in keep {
                    let w = weights[k] / sum, b = states[k][block]
                    box.x += b.x * w; box.y += b.y * w; box.w += b.w * w; box.h += b.h * w
                }
                boxes[block] = box
            }
        }
        // The carried tile is lifted off the layout: it rides under the pointer at the nearest state's size.
        boxes[dragged] = TileBox(x: anchor.x, y: anchor.y, w: near[dragged].w, h: near[dragged].h)
        return Frame(boxes: boxes, nearest: slots[ni], nearestAnchor: near[dragged].origin)
    }

    /// Where the carried tile's top left stands when it takes `slot`.
    public static func stop(order: [Int], dragged: Int, slot: Int, metrics: TileMetrics) -> CGPoint {
        view(order: Self.order(order, moving: dragged, to: slot), metrics: metrics)[dragged].origin
    }
}
