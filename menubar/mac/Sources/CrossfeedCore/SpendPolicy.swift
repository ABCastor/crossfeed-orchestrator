import Foundation

public enum SpendLevel: String, CaseIterable, Hashable {
    case off, low, normal, high, forced

    public var label: String {
        self == .forced ? "Max" : rawValue.capitalized
    }
}

public struct PolicyAttribution: Decodable, Equatable {
    public enum Actor: String, Decodable {
        case operator_, agent

        public init(from decoder: Decoder) throws {
            let value = try decoder.singleValueContainer().decode(String.self)
            switch value {
            case "owner", "operator": self = .operator_
            case "agent": self = .agent
            default: throw GlanceError.malformed
            }
        }
    }

    public let actor: Actor
    public let when: String

    enum CodingKeys: String, CodingKey { case actor, when, setBy = "set_by", setAt = "set_at" }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        actor = try c.decodeIfPresent(Actor.self, forKey: .setBy) ?? c.decode(Actor.self, forKey: .actor)
        when = try c.decodeIfPresent(String.self, forKey: .setAt) ?? c.decode(String.self, forKey: .when)
    }

    public func line(now: Date) -> String {
        let who = actor == .operator_ ? "Set by you" : "Set by an agent"
        guard let date = Presenter.controlDate(when) else { return who }
        let age = max(0, now.timeIntervalSince(date))
        if age < 60 { return who + " · just now" }
        if age < 3600 { return who + " · \(Int(age / 60)) min ago" }
        if age < 86400 { return who + " · \(Int(age / 3600)) h ago" }
        return who + " · \(date.formatted(.dateTime.month(.abbreviated).day().hour().minute()))"
    }
}
