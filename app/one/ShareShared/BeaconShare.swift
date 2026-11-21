import Foundation

/// What the share sheet and the app share: where Beacon is, its key, and saves waiting for the Mac.
///
/// The share extension can't read the app's own settings, so the app copies the server and key into the
/// app group each time it starts. A save made while the Mac can't be reached waits in the group too, and the
/// app sends it the next time it opens.
enum BeaconShare {
    static let group = "group.com.example.beacon"
    static let serverKey = "shareServer"
    static let accessKey = "shareKey"
    static let pendingKey = "sharePending"

    static var shared: UserDefaults? { UserDefaults(suiteName: group) }

    struct Project: Codable, Identifiable, Hashable {
        let slug: String
        let name: String
        let matches: Int
        let recent: Int
        var id: String { slug }
    }

    struct Save: Codable, Equatable {
        let url: String
        let projects: [String]
        let note: String
    }

    struct Failure: LocalizedError {
        let errorDescription: String?
    }

    /// Called by the app: copy Settings › Beacon (or a pairing) into the group, for the share sheet.
    static func publish(server: String?, key: String?) {
        guard let shared else { return }
        shared.set(server, forKey: serverKey)
        shared.set(key, forKey: accessKey)
    }

    static func base(builtIn: String?) -> URL? {
        for text in [shared?.string(forKey: serverKey), builtIn] {
            if let text = text?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty,
               let url = URL(string: text), url.host != nil { return url }
        }
        return nil
    }

    static func projects(text: String, base: URL) async throws -> [Project] {
        var components = URLComponents(url: base.appending(path: "projects"), resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "text", value: String(text.prefix(1500)))]
        let data = try await send(URLRequest(url: components.url!, timeoutInterval: 8))
        return try JSONDecoder().decode([Project].self, from: data)
    }

    static func send(_ save: Save, base: URL) async throws {
        // An X link is read with bird before Beacon answers, so this waits longer than a plain page.
        var request = URLRequest(url: base.appending(path: "add"), timeoutInterval: 45)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["url": save.url, "projects": save.projects, "note": save.note])
        _ = try await send(request)
    }

    private static func send(_ request: URLRequest) async throws -> Data {
        var request = request
        if let key = shared?.string(forKey: accessKey)?.trimmingCharacters(in: .whitespacesAndNewlines), !key.isEmpty {
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }
        let data: Data, response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw Failure(errorDescription: "Can't reach Beacon on the Mac.")
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 { throw Failure(errorDescription: "Open Beacon once so the share sheet gets its access key.") }
        guard status < 300 else {
            let message = (try? JSONDecoder().decode([String: String].self, from: data))?["error"]
            throw Failure(errorDescription: message ?? "Beacon on the Mac returned an error.")
        }
        return data
    }

    // MARK: Saves waiting for the Mac

    static var pending: [Save] {
        guard let data = shared?.data(forKey: pendingKey) else { return [] }
        return (try? JSONDecoder().decode([Save].self, from: data)) ?? []
    }

    static func hold(_ save: Save) {
        let saves = pending + [save]
        shared?.set(try? JSONEncoder().encode(saves), forKey: pendingKey)
    }

    /// Called by the app when it opens: send what waited. Returns how many went through.
    @discardableResult
    static func flush(base: URL) async -> Int {
        var sent: [Save] = []
        for save in pending {
            if (try? await send(save, base: base)) != nil { sent.append(save) }
        }
        // Read again: the share sheet may have held another save while these were sending.
        var left = pending
        for save in sent { if let index = left.firstIndex(of: save) { left.remove(at: index) } }
        shared?.set(try? JSONEncoder().encode(left), forKey: pendingKey)
        return sent.count
    }
}
