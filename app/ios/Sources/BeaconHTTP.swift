import Foundation

/// Calls `beacon serve`: from the phone over Tailscale, and from the Mac's Library on this Mac.
struct BeaconHTTP: Backend {
    static let serverKey = "beaconServer"
    /// The phone's access key (`beacon serve --show-key` on the Mac), entered in Settings › Beacon.
    static let accessKeyKey = "beaconKey"
    /// Where the phone looks before anything is entered in Settings › Beacon: the address
    /// `scripts/install-phone` built in (the owner's Mac), or a placeholder in public builds.
    static let placeholderServer = "http://your-mac.your-tailnet.ts.net:8765"
    static var defaultServer: String {
        nonEmpty(Bundle.main.object(forInfoDictionaryKey: "BeaconDefaultServer") as? String) ?? placeholderServer
    }

    /// A blank Settings field counts as unset, so it can't turn into an invalid URL.
    static func nonEmpty(_ text: String?) -> String? {
        guard let trimmed = text?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else { return nil }
        return trimmed
    }

    /// The server the phone uses: Settings › Beacon if filled in, then the built-in default.
    static func phoneServer(setting: String?, builtIn: String) -> URL {
        for candidate in [nonEmpty(setting), nonEmpty(builtIn)] {
            if let text = candidate, let url = URL(string: text), url.host != nil { return url }
        }
        return URL(string: placeholderServer)!
    }

    struct Failure: LocalizedError {
        let errorDescription: String?
    }

    /// Set on the Mac (this Mac's server); nil on the phone, which reads the server from Settings.
    var server: URL?

    var base: URL {
        server ?? Self.phoneServer(setting: UserDefaults.standard.string(forKey: Self.serverKey), builtIn: Self.defaultServer)
    }

    func queue() async throws -> [Item] { try await get("queue") }
    func plans() async throws -> [Plan] { try await get("briefs") }

    func library(offset: Int, limit: Int, area: String?, search: LibrarySearch?) async throws -> LibraryPage {
        var url = URLComponents(url: base.appending(path: "library"), resolvingAgainstBaseURL: false)!
        url.queryItems = [URLQueryItem(name: "offset", value: String(offset)), URLQueryItem(name: "limit", value: String(limit))]
        if let area { url.queryItems?.append(URLQueryItem(name: "area", value: area)) }
        if let search { url.queryItems?.append(contentsOf: search.queryItems) }
        return try JSONDecoder().decode(LibraryPage.self, from: await send(URLRequest(url: url.url!)))
    }

    func compose(search: String, ids: [Int]) async throws -> ComposeResult {
        try JSONDecoder().decode(ComposeResult.self, from: await post("library/compose", ["search": search, "ids": ids], timeout: 90))
    }

    func composeChoice(id: Int, choice: String) async throws {
        try await post("library/compose/choice", ["id": id, "choice": choice])
    }

    func keep(url: String) async throws {
        try await post("add", ["url": url], timeout: 120)
    }

    func keep(picture: Data, width: Int, height: Int, note: String) async throws {
        try await post("add", ["image": picture.base64EncodedString(), "width": width, "height": height, "note": note], timeout: 120)
    }

    func references(brief: String, ids: [Int], words: Bool) async throws -> ReferenceReview {
        try JSONDecoder().decode(ReferenceReview.self, from: await post("references", ["brief": brief, "ids": ids, "words": words], timeout: 120))
    }

    func handled() async throws -> [HandledItem] { try await get("handled") }

    func unhandle(id: Int) async throws {
        try await post("unhandle", ["id": id])
    }

    func mark(_ status: String, ids: [Int]) async throws {
        try await post("mark", ["status": status, "ids": ids])
    }

    func read(id: Int) async throws {
        try await post("read", ["id": id])
    }

    func plan(project: String) async throws {
        // The Mac holds the request until the agent finishes.
        try await post("plan", ["project": project], timeout: 900)
    }

    func setBriefStatus(_ brief: Brief, to status: String) async throws {
        try await post("brief-status", ["item_id": brief.itemId, "workstream": brief.workstream, "status": status])
    }

    func finishBrief(_ brief: Brief, itemStatus: String) async throws {
        try await post("brief-done", ["item_id": brief.itemId, "workstream": brief.workstream, "item_status": itemStatus])
    }

    func setForMac(_ brief: Brief, _ on: Bool) async throws {
        try await post("brief-mac", ["item_id": brief.itemId, "workstream": brief.workstream, "on": on])
    }

    func activity() async throws -> [Activation] { try await get("activity") }

    /// The server answers at once and runs the agent in its own background thread.
    func activate(itemId: Int) async throws -> Activation? {
        try JSONDecoder().decode(Activation.self, from: await post("activate", ["item_id": itemId]))
    }

    func answer(activation id: Int, text: String) async throws -> Activation? {
        try JSONDecoder().decode(Activation.self, from: await post("activity/\(id)", ["answer": text]))
    }

    func close(activation id: Int, as state: String) async throws -> Activation {
        try JSONDecoder().decode(Activation.self, from: await post("activity/\(id)", ["state": state]))
    }

    func callSession(size: Int, new: Bool) async throws -> CallSession {
        try await get("calls/session", query: ["size": String(size), "new": new ? "1" : "0"])
    }

    func checkCall(session: LooseID, card: LooseID, answer: String, destination: String?, chip: String?) async throws -> CallCheck {
        var body: [String: Any] = ["session_id": session.json, "card_id": card.json, "answer": answer]
        if let destination { body["destination"] = destination }
        if let chip { body["chip"] = chip }
        return try JSONDecoder().decode(CallCheck.self, from: await post("calls/check", body))
    }

    func undoCall(change: LooseID) async throws {
        try await post("calls/undo", ["change_id": change.json])
    }

    func callSummary(session: LooseID?) async throws -> CallSummary {
        try await get("calls/summary", query: session.map { ["session": $0.description] } ?? [:])
    }

    func redecide(group: String) async throws {
        try await post("calls/redecide", ["group": group])
    }

    func pick(round id: Int, position: Int, choice: String, why: String?, shownAt: String?) async throws -> Int? {
        var body: [String: Any] = ["round_id": id, "position": position, "choice": choice]
        if let why { body["why"] = why }
        if let shownAt { body["shown_at"] = shownAt }
        return try JSONDecoder().decode(PickRecorded.self, from: await post("pick", body)).nextPosition
    }

    func undoPick(round id: Int) async throws -> Int? {
        try JSONDecoder().decode(PickUndone.self, from: await post("pick/undo", ["round_id": id])).position
    }

    /// The pipeline runs on the Mac's schedule or its Sync button; a phone request would outlive the connection.
    func sync() async throws {
        throw Failure(errorDescription: "Sync runs from the Mac app.")
    }

    private func get<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        var url = URLComponents(url: base.appending(path: path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty { url.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) } }
        return try JSONDecoder().decode(T.self, from: await send(URLRequest(url: url.url!)))
    }

    @discardableResult
    private func post(_ path: String, _ body: [String: Any], timeout: TimeInterval = 60) async throws -> Data {
        var request = URLRequest(url: base.appending(path: path), timeoutInterval: timeout)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        return try await send(request)
    }

    private func send(_ request: URLRequest) async throws -> Data {
        var request = request
        // This Mac asking for itself needs no key. The phone sends its own; pictures are loaded
        // by image views and don't need one.
        if server == nil, let key = UserDefaults.standard.string(forKey: Self.accessKeyKey)?.trimmingCharacters(in: .whitespacesAndNewlines), !key.isEmpty {
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }
        let data: Data, response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw Failure(errorDescription: server != nil
                ? "Beacon's server on this Mac isn't running. It restarts by itself; try again in a moment."
                : "Can't reach Beacon on the Mac. Check that Tailscale is on and the Mac is awake.")
        }
        if (response as? HTTPURLResponse)?.statusCode == 401 {
            throw Failure(errorDescription: "Beacon needs its access key. On the Mac, choose Edit › Copy Key for iPhone, then tap Paste Access Key.")
        }
        guard let status = (response as? HTTPURLResponse)?.statusCode, status < 300 else {
            let message = (try? JSONDecoder().decode([String: String].self, from: data))?["error"]
            throw Failure(errorDescription: message ?? "Beacon on the Mac returned an error.")
        }
        return data
    }
}
