import Foundation

public enum ConsolePage: Equatable {
    case providers
    case about
    /// The cost and usage page, at one provider's section when a pool id is given (CONTRACT: /usage#<pool>).
    case usage(pool: String?)

    /// Accept only the startup link from fleetctl's localhost console. Never log the line or URL.
    public static func signedURL(from line: String) -> URL? {
        let prefix = "Sign-in link for this launch: "
        guard line.hasPrefix(prefix),
              let parts = URLComponents(string: String(line.dropFirst(prefix.count))),
              parts.scheme == "http", parts.host == "127.0.0.1",
              let port = parts.port, (1...65_535).contains(port),
              parts.path == "/login", parts.user == nil, parts.password == nil, parts.fragment == nil,
              let query = parts.queryItems, query.count == 1, query[0].name == "key",
              let key = query[0].value, !key.isEmpty else { return nil }
        return parts.url
    }

    public func url(from signedURL: URL) -> URL? {
        var parts = URLComponents(url: signedURL, resolvingAgainstBaseURL: false)
        // The sign-in's 303 Location has no fragment, so the browser carries this one to the page.
        switch self {
        case .providers: parts?.fragment = nil
        case .about: parts?.fragment = "about"
        case .usage(let pool):
            // `next` names the page the console lands on after sign-in; it is a fixed path, never taken from a reading.
            let items = (parts?.queryItems ?? []) + [URLQueryItem(name: "next", value: "/usage")]
            let fragment = pool.flatMap(Self.safeFragment)
            parts?.queryItems = items
            parts?.fragment = fragment
        }
        return parts?.url
    }

    /// A pool id is lowercase letters, digits and hyphens; anything else is dropped rather than put in a link.
    static func safeFragment(_ id: String) -> String? {
        guard !id.isEmpty, id.count <= 64,
              id.allSatisfy({ $0.isASCII && ($0.isLowercase || $0.isNumber || $0 == "-") }) else { return nil }
        return id
    }
}
