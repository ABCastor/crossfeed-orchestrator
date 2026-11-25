import Foundation

public enum SpendLevel: String, CaseIterable, Hashable {
    case off, low, normal, high, forced

    public var label: String {
        self == .forced ? "Ignore estimates" : rawValue.capitalized
    }
}

public struct PolicyAttribution: Decodable, Equatable {
    public enum Actor: String, Decodable {
        case operator_ = "owner" // existing fleetctl audit vocabulary
        case agent
    }

    public let actor: Actor
    public let when: String

    public func line(now: Date) -> String {
        let who = actor == .operator_ ? "Set by you" : "Set by an agent"
        guard let date = Presenter.iso.date(from: when) else { return who }
        let age = max(0, now.timeIntervalSince(date))
        if age < 60 { return who + " · just now" }
        if age < 3600 { return who + " · \(Int(age / 60)) min ago" }
        if age < 86400 { return who + " · \(Int(age / 3600)) h ago" }
        return who + " · \(date.formatted(.dateTime.month(.abbreviated).day().hour().minute()))"
    }
}
