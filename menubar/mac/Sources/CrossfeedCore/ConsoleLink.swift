import Foundation

public enum ConsolePage: String {
    case providers
    case about

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
        parts?.fragment = self == .about ? "about" : nil
        return parts?.url
    }
}
