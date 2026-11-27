// Crossfeed menu: the tiles of every tab reorder by drag, in two dimensions (B+ pairs two half tiles in a row).
//
// Event path: an open NSMenu delivers clicks to the hosting view, not to SwiftUI gestures, so the host's mouseDown /
// mouseDragged / mouseUp feed MenuState (beginPointer / movePointer / endPointer). The pointer moves the carried
// tile's anchor; TileLayout draws every frame from it as TileMotion's blend of legal orders. The keyboard and
// screen-reader path is a context menu and accessibility actions.
import AppKit
import CrossfeedCore
import SwiftUI

/// Where one tile is, in the menu's coordinates, and which tile it is among its tab's.
public struct BlockFrame: Equatable {
    public var tab: String
    public var index: Int
    public var rect: CGRect
}

struct BlockFrames: PreferenceKey {
    static var defaultValue: [String: BlockFrame] = [:]
    static func reduce(value: inout [String: BlockFrame], nextValue: () -> [String: BlockFrame]) {
        value.merge(nextValue()) { $1 }
    }
}

/// What the layout last measured for a tab, so a drag can find the nearest slot without asking SwiftUI.
final class MetricsProbe {
    var metrics: TileMetrics?
}

/// One tile being carried, or settling after release or cancel.
public struct BlockDrag: Equatable {
    public var tab: String
    public var id: String
    public var ids: [String]          // the tab's tiles, in the order the view declares them
    public var order: [Int]           // order[slot] = tile index, as it stood when the drag began
    public var index: Int             // the carried tile's index in `ids`
    public var startOrigin: CGPoint   // the carried tile's top left in the tab's own coordinates
    public var press: CGPoint
    /// The carried tile's top left, in the tab's own coordinates. Follows the pointer 1:1 while carried.
    public var anchor: CGPoint
    public var metrics: TileMetrics
    public var legal: [Int]
    public var settling = false
    public var startSlot: Int { order.firstIndex(of: index) ?? 0 }
}

extension MenuState {
    func block(at point: CGPoint) -> String? {
        blockFrames.first { $0.value.rect.contains(point) }?.key
    }

    var reducedMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }

    var settleAnimation: Animation {
        .timingCurve(0.2, 0.8, 0.2, 1, duration: reducedMotion ? BlockMotion.settleReduced : BlockMotion.settle)
    }

    /// A tile is held by the pointer (not yet released).
    public var isCarryingBlock: Bool { blockDrag?.settling == false }

    private func origin(of tab: String) -> CGPoint? {
        let rects = blockFrames.values.filter { $0.tab == tab }.map(\.rect)
        guard let x = rects.map(\.minX).min(), let y = rects.map(\.minY).min() else { return nil }
        return CGPoint(x: x, y: y)
    }

    /// Takes hold of a tile. Returns false when it cannot be found or another tile is already being carried.
    @discardableResult
    func beginBlockDrag(_ id: String, press: CGPoint) -> Bool {
        guard blockDrag == nil, let frame = blockFrames[id] else { return false }
        let tab = frame.tab
        let all = blockFrames.filter { $0.value.tab == tab }.sorted { $0.value.index < $1.value.index }
        guard all.count > 1, let index = all.firstIndex(where: { $0.key == id }), let origin = origin(of: tab),
              let metrics = probes[tab]?.metrics, metrics.full.count == all.count else { return false }
        let ids = all.map(\.key)
        let order = Array(all.indices)   // the view declares the tiles in the order they are on screen
        let start = CGPoint(x: frame.rect.minX - origin.x, y: frame.rect.minY - origin.y)
        let legal = TileMotion.legalSlots(order: order, dragged: index, group: groupIndices(tab: tab, ids: ids))
        blockDrag = BlockDrag(tab: tab, id: id, ids: ids, order: order, index: index, startOrigin: start, press: press,
                              anchor: start, metrics: metrics, legal: legal)
        onBlockDragActive(true)
        return true
    }

    /// For review images: carry tile `index` of `tab` by (dx, dy) as a pointer would, and leave it held.
    public func simulateDrag(tab: String, index: Int, dx: CGFloat, dy: CGFloat) {
        let all = blockFrames.filter { $0.value.tab == tab }.sorted { $0.value.index < $1.value.index }
        guard all.indices.contains(index) else { return }
        let rect = all[index].value.rect
        let press = CGPoint(x: rect.minX + 20, y: rect.minY + 6)
        guard beginBlockDrag(all[index].key, press: press) else { return }
        moveBlockDrag(to: CGPoint(x: press.x + dx, y: press.y + dy))
    }

    /// Overview's provider tiles stay together: one group.
    private func groupIndices(tab: String, ids: [String]) -> [Int?]? {
        guard tab == Presenter.overviewID else { return nil }
        return ids.map { $0.hasPrefix("provider:") ? 0 : nil }
    }

    /// Follow the pointer: the tile's top left is where it was grabbed plus the pointer's travel.
    func moveBlockDrag(to point: CGPoint) {
        guard var drag = blockDrag, !drag.settling else { return }
        drag.anchor = CGPoint(x: drag.startOrigin.x + point.x - drag.press.x, y: drag.startOrigin.y + point.y - drag.press.y)
        blockDrag = drag
    }

    /// Release: settle on the nearest legal order and write it.
    func releaseBlockDrag() {
        guard let drag = blockDrag, !drag.settling else { return }
        let nearest = TileMotion.frame(order: drag.order, dragged: drag.index, anchor: drag.anchor, metrics: drag.metrics, legal: drag.legal).nearest
        settleBlockDrag(toSlot: nearest, commit: true)
    }

    /// Escape, the menu closing, the pointer lost: settle back where it began and write nothing.
    public func cancelBlockDrag() {
        guard let drag = blockDrag, !drag.settling else { return }
        pressedBlock = nil   // the rest of this press (moves, the release) must not take hold again
        settleBlockDrag(toSlot: drag.startSlot, commit: false)
    }

    /// Move a tile one place by key, menu or screen reader: the same drag, driven to the next legal state.
    public func moveBlock(_ id: String, by step: Int) {
        guard blockDrag == nil, beginBlockDrag(id, press: .zero), let drag = blockDrag else { return }
        let legal = drag.legal
        let from = legal.firstIndex(of: drag.startSlot) ?? 0
        guard legal.indices.contains(from + step) else {
            blockDrag = nil
            onBlockDragActive(false)
            return
        }
        let slot = legal[from + step]
        // One turn of the run loop so the tile is drawn at its own place before it is driven away.
        DispatchQueue.main.async { self.settleBlockDrag(toSlot: slot, commit: true) }
    }

    public func canMoveBlock(_ id: String, by step: Int) -> Bool {
        guard let frame = blockFrames[id] else { return false }
        let all = blockFrames.filter { $0.value.tab == frame.tab }.sorted { $0.value.index < $1.value.index }
        let ids = all.map(\.key)
        guard let index = ids.firstIndex(of: id) else { return false }
        let order = Array(all.indices)
        let legal = TileMotion.legalSlots(order: order, dragged: index, group: groupIndices(tab: frame.tab, ids: ids))
        guard let slot = order.firstIndex(of: index), let at = legal.firstIndex(of: slot) else { return false }
        return legal.indices.contains(at + step)
    }

    private func settleBlockDrag(toSlot slot: Int, commit: Bool) {
        guard var drag = blockDrag, !drag.settling else { return }
        let target = TileMotion.stop(order: drag.order, dragged: drag.index, slot: slot, metrics: drag.metrics)
        let duration = reducedMotion ? BlockMotion.settleReduced : BlockMotion.settle
        drag.settling = true
        drag.anchor = target
        withAnimation(settleAnimation) { blockDrag = drag }
        DispatchQueue.main.asyncAfter(deadline: .now() + duration + 0.03) { [weak self] in
            guard let self, let drag = self.blockDrag else { return }
            var transaction = Transaction(animation: nil)
            transaction.disablesAnimations = true
            withTransaction(transaction) {
                if commit, slot != drag.startSlot {
                    let order = TileMotion.order(drag.order, moving: drag.index, to: slot).map { drag.ids[$0] }
                    self.onReorderBlocks(drag.tab, order)
                }
                self.blockDrag = nil
            }
            self.onBlockDragActive(false)
        }
    }
}

struct HalfKey: LayoutValueKey { static let defaultValue = false }

/// Places its subviews from an order: a pair of half tiles shares a row, the carried tile rides at `anchor`, the rest sit
/// where the blend of the legal orders puts them. `anchor` is what animates when a drag settles.
struct TileLayout: Layout {
    var order: [Int]
    var dragged: Int?
    var anchor: CGPoint
    var legal: [Int]?
    var probe: MetricsProbe?
    var gap: CGFloat = TileMotion.gap

    var animatableData: AnimatablePair<CGFloat, CGFloat> {
        get { AnimatablePair(anchor.x, anchor.y) }
        set { anchor = CGPoint(x: newValue.first, y: newValue.second) }
    }

    private func metrics(_ subviews: Subviews, width: CGFloat) -> TileMetrics {
        let half = subviews.map { $0[HalfKey.self] }
        let halfWidth = (width - gap) / 2
        let full = subviews.map { $0.sizeThatFits(ProposedViewSize(width: width, height: nil)).height }
        let halves = subviews.enumerated().map { i, view in
            half[i] ? view.sizeThatFits(ProposedViewSize(width: halfWidth, height: nil)).height : full[i]
        }
        return TileMetrics(half: half, full: full, halfHeight: halves, width: width, gap: gap)
    }

    private func validOrder(_ count: Int) -> [Int] {
        order.count == count && Set(order) == Set(0..<count) ? order : Array(0..<count)
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 310
        let m = metrics(subviews, width: width)
        return CGSize(width: width, height: TileMotion.height(order: validOrder(subviews.count), metrics: m))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let m = metrics(subviews, width: bounds.width)
        probe?.metrics = m
        let order = validOrder(subviews.count)
        let boxes: [TileBox]
        if let dragged, order.contains(dragged) {
            boxes = TileMotion.frame(order: order, dragged: dragged, anchor: anchor, metrics: m, legal: legal).boxes
        } else {
            boxes = TileMotion.view(order: order, metrics: m)
        }
        for (index, subview) in subviews.enumerated() {
            let box = boxes[index]
            subview.place(at: CGPoint(x: bounds.minX + box.x, y: bounds.minY + box.y), anchor: .topLeading,
                          proposal: ProposedViewSize(width: box.w, height: box.h))
        }
    }
}

struct TileItem: Identifiable {
    let id: String
    var half = false
    let content: AnyView
}

struct ReorderableTiles: View {
    let tab: String
    let tiles: [TileItem]
    @ObservedObject var state: MenuState
    /// The settings list stacks rows with a smaller gap and no lift scale change in width.
    var gap: CGFloat = TileMotion.gap

    var body: some View {
        let ids = tiles.map(\.id)
        let order = Array(ids.indices)
        let drag = state.blockDrag?.tab == tab ? state.blockDrag : nil
        TileLayout(order: drag?.order ?? order, dragged: drag?.index, anchor: drag?.anchor ?? .zero,
                   legal: drag?.legal, probe: state.probe(for: tab), gap: gap) {
            ForEach(Array(tiles.enumerated()), id: \.element.id) { index, tile in
                let carried = drag?.index == index
                let lifted = carried && drag?.settling == false
                tile.content
                    .layoutValue(key: HalfKey.self, value: tile.half)
                    // The carried tile is opaque: its own card colour under a little ink, so what it passes over never shows through.
                    .background(RoundedRectangle(cornerRadius: 8).fill(carried ? CF.tile : Color.clear))
                    .background(GeometryReader { geo in
                        Color.clear.preference(key: BlockFrames.self,
                                               value: [tile.id: BlockFrame(tab: tab, index: index, rect: geo.frame(in: .named(MenuMetrics.space)))])
                    })
                    .overlay(alignment: .topLeading) {
                        // The grip shows on the carried tile: it is the thing in hand.
                        Image(systemName: "circle.grid.2x3.fill").font(.system(size: 6)).foregroundStyle(CF.ink)
                            .padding(.leading, 3).padding(.top, 8).opacity(lifted ? 1 : 0).allowsHitTesting(false).accessibilityHidden(true)
                    }
                    .scaleEffect(lifted && !state.reducedMotion ? 1.02 : 1)
                    .shadow(color: CF.liftShadow.opacity(lifted ? 1 : 0), radius: lifted ? 12 : 0, y: lifted ? 4 : 0)
                    .animation(state.settleAnimation, value: lifted)
                    .zIndex(carried ? 1 : 0)
                    .contextMenu {
                        Button("Move up") { state.moveBlock(tile.id, by: -1) }.disabled(!state.canMoveBlock(tile.id, by: -1))
                        Button("Move down") { state.moveBlock(tile.id, by: 1) }.disabled(!state.canMoveBlock(tile.id, by: 1))
                    }
                    .accessibilityAction(named: "Move up") { state.moveBlock(tile.id, by: -1) }
                    .accessibilityAction(named: "Move down") { state.moveBlock(tile.id, by: 1) }
            }
        }
    }
}
