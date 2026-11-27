// Crossfeed menu: the blocks of a provider card (windows, spend policy, status) reorder by drag.
//
// Event path: an open NSMenu delivers clicks to the hosting view, not to SwiftUI gestures, so the host's
// mouseDown / mouseDragged / mouseUp feed MenuState (beginPointer / movePointer / endPointer), which turns the
// pointer's y into `blockDrag.top`. BlockLayout draws every frame from it as a blend of legal orders
// (see BlockMotion). The keyboard and screen-reader path is a context menu and accessibility actions.
import AppKit
import CrossfeedCore
import SwiftUI

/// Where one block is, in the menu's coordinates, and which block it is among its tab's.
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

/// One block being carried, or settling after release or cancel.
public struct BlockDrag: Equatable {
    public var tab: String
    public var id: String
    public var ids: [String]          // the tab's blocks, in the order the view declares them
    public var heights: [CGFloat]
    public var order: [Int]           // order[slot] = block index, as it stood when the drag began
    public var index: Int             // the carried block's index in `ids`
    public var startTop: CGFloat
    public var pressY: CGFloat
    /// The carried block's top, in the card's own coordinates. Follows the pointer 1:1 while carried.
    public var top: CGFloat
    public var settling = false
    public var startSlot: Int { order.firstIndex(of: index) ?? 0 }
}

extension MenuState {
    /// The blocks of `tab` in the order to draw them: the saved order over what is there now.
    func blockOrder(_ tab: String, ids: [String]) -> [String] {
        SectionOrderStore.merged(saved: blockOrders[tab] ?? [], available: ids)
    }

    func block(at point: CGPoint) -> String? {
        blockFrames.first { $0.value.rect.contains(point) }?.key
    }

    var reducedMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }

    var settleAnimation: Animation {
        .timingCurve(0.2, 0.8, 0.2, 1, duration: reducedMotion ? BlockMotion.settleReduced : BlockMotion.settle)
    }

    /// A block is held by the pointer (not yet released).
    public var isCarryingBlock: Bool { blockDrag?.settling == false }

    /// Takes hold of a block. Returns false when it cannot be found or another block is already being carried.
    @discardableResult
    func beginBlockDrag(_ id: String, pressY: CGFloat) -> Bool {
        guard blockDrag == nil, let frame = blockFrames[id] else { return false }
        let tab = frame.tab
        let all = blockFrames.filter { $0.value.tab == tab }.sorted { $0.value.index < $1.value.index }
        guard all.count > 1, let index = all.firstIndex(where: { $0.key == id }), let origin = all.map(\.value.rect.minY).min() else { return false }
        let ids = all.map(\.key)
        let order = blockOrder(tab, ids: ids).compactMap { ids.firstIndex(of: $0) }
        let startTop = frame.rect.minY - origin
        blockDrag = BlockDrag(tab: tab, id: id, ids: ids, heights: all.map { $0.value.rect.height }, order: order,
                              index: index, startTop: startTop, pressY: pressY, top: startTop)
        onBlockDragActive(true)
        return true
    }

    /// Follow the pointer: the block's top is where it was grabbed plus the pointer's travel, held to the legal slots.
    func moveBlockDrag(toY y: CGFloat) {
        guard var drag = blockDrag, !drag.settling else { return }
        let free = drag.startTop + (y - drag.pressY)
        drag.top = BlockMotion.frame(order: drag.order, dragged: drag.index, top: free, heights: drag.heights).tops[drag.index]
        blockDrag = drag
    }

    /// Release: settle on the nearest legal order and write it.
    func releaseBlockDrag() {
        guard let drag = blockDrag, !drag.settling else { return }
        let nearest = BlockMotion.frame(order: drag.order, dragged: drag.index, top: drag.top, heights: drag.heights).nearest
        settleBlockDrag(toSlot: nearest, commit: true)
    }

    /// Escape, the menu closing, the pointer lost: settle back where it began and write nothing.
    public func cancelBlockDrag() {
        guard let drag = blockDrag, !drag.settling else { return }
        pressedBlock = nil   // the rest of this press (moves, the release) must not take hold again
        settleBlockDrag(toSlot: drag.startSlot, commit: false)
    }

    /// Move a block one place by key, menu or screen reader: the same drag, driven to the next state.
    public func moveBlock(_ id: String, by step: Int) {
        guard blockDrag == nil, beginBlockDrag(id, pressY: 0), let drag = blockDrag else { return }
        let slot = drag.startSlot + step
        guard drag.order.indices.contains(slot) else {
            blockDrag = nil
            onBlockDragActive(false)
            return
        }
        // One turn of the run loop so the block is drawn at its own place before it is driven away.
        DispatchQueue.main.async { self.settleBlockDrag(toSlot: slot, commit: true) }
    }

    public func canMoveBlock(_ id: String, by step: Int) -> Bool {
        guard let frame = blockFrames[id] else { return false }
        let all = blockFrames.filter { $0.value.tab == frame.tab }.sorted { $0.value.index < $1.value.index }
        let ids = all.map(\.key)
        guard let slot = blockOrder(frame.tab, ids: ids).firstIndex(of: id) else { return false }
        return (0..<ids.count).contains(slot + step)
    }

    private func settleBlockDrag(toSlot slot: Int, commit: Bool) {
        guard var drag = blockDrag, !drag.settling else { return }
        let target = BlockMotion.stop(order: drag.order, dragged: drag.index, slot: slot, heights: drag.heights)
        let duration = reducedMotion ? BlockMotion.settleReduced : BlockMotion.settle
        drag.settling = true
        drag.top = target
        withAnimation(settleAnimation) { blockDrag = drag }
        DispatchQueue.main.asyncAfter(deadline: .now() + duration + 0.03) { [weak self] in
            guard let self, let drag = self.blockDrag else { return }
            var transaction = Transaction(animation: nil)
            transaction.disablesAnimations = true
            withTransaction(transaction) {
                if commit, slot != drag.startSlot {
                    let order = BlockMotion.order(drag.order, moving: drag.index, to: slot).map { drag.ids[$0] }
                    self.blockOrders[drag.tab] = order
                    self.onReorderBlocks(drag.tab, order)
                }
                self.blockDrag = nil
            }
            self.onBlockDragActive(false)
        }
    }
}

/// Places its subviews top to bottom in `order`, the carried one at `top`, the rest where the blend of the
/// two legal orders either side of it puts them. `top` is what animates when a drag settles.
struct BlockLayout: Layout {
    var order: [Int]
    var dragged: Int?
    var top: CGFloat
    var gap: CGFloat = BlockMotion.gap

    var animatableData: CGFloat {
        get { top }
        set { top = newValue }
    }

    private func heights(_ subviews: Subviews, width: CGFloat) -> [CGFloat] {
        subviews.map { $0.sizeThatFits(ProposedViewSize(width: width, height: nil)).height }
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 310
        let h = heights(subviews, width: width)
        return CGSize(width: width, height: h.reduce(0, +) + gap * CGFloat(max(0, h.count - 1)))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let h = heights(subviews, width: bounds.width)
        let valid = order.count == h.count && Set(order) == Set(h.indices)
        let order = valid ? order : Array(h.indices)
        let tops: [CGFloat]
        if let dragged, order.contains(dragged) {
            tops = BlockMotion.frame(order: order, dragged: dragged, top: top, heights: h, gap: gap).tops
        } else {
            tops = BlockMotion.tops(order: order, heights: h, gap: gap)
        }
        for (index, subview) in subviews.enumerated() {
            subview.place(at: CGPoint(x: bounds.minX, y: bounds.minY + tops[index]), anchor: .topLeading,
                          proposal: ProposedViewSize(width: bounds.width, height: h[index]))
        }
    }
}

struct Block: Identifiable {
    let id: String
    let title: String
    let content: AnyView
}

struct ReorderableBlocks: View {
    let tab: String
    let blocks: [Block]
    @ObservedObject var state: MenuState

    var body: some View {
        let ids = blocks.map(\.id)
        let order = state.blockOrder(tab, ids: ids).compactMap { ids.firstIndex(of: $0) }
        let drag = state.blockDrag?.tab == tab ? state.blockDrag : nil
        BlockLayout(order: order, dragged: drag?.index, top: drag?.top ?? 0) {
            ForEach(Array(blocks.enumerated()), id: \.element.id) { index, block in
                let carried = drag?.index == index
                let lifted = carried && drag?.settling == false
                block.content
                    // The carried block covers what it passes over: opaque paper with a little ink, close to the panel's tone.
                    .background(carried ? CF.ink.opacity(0.12) : Color.clear)
                    .background(carried ? CF.paper : Color.clear)
                    .background(GeometryReader { geo in
                        Color.clear.preference(key: BlockFrames.self,
                                               value: [block.id: BlockFrame(tab: tab, index: index, rect: geo.frame(in: .named(MenuMetrics.space)))])
                    })
                    .scaleEffect(lifted && !state.reducedMotion ? 1.02 : 1)
                    .shadow(color: .black.opacity(lifted ? 0.16 : 0), radius: lifted ? 9 : 0, y: lifted ? 3 : 0)
                    .animation(state.settleAnimation, value: lifted)
                    .zIndex(carried ? 1 : 0)
                    .contextMenu {
                        Button("Move up") { state.moveBlock(block.id, by: -1) }.disabled(!state.canMoveBlock(block.id, by: -1))
                        Button("Move down") { state.moveBlock(block.id, by: 1) }.disabled(!state.canMoveBlock(block.id, by: 1))
                    }
                    .accessibilityAction(named: "Move up") { state.moveBlock(block.id, by: -1) }
                    .accessibilityAction(named: "Move down") { state.moveBlock(block.id, by: 1) }
            }
        }
    }
}
