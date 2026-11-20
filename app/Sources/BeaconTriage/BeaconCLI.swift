import Foundation

/// Talks to Beacon's Python CLI, which stays the only writer so backlog files
/// regenerate exactly as they do from the terminal.
actor BeaconCLI: Backend {
    static let repoKey = "beaconRepoPath"

    enum Failure: LocalizedError {
        case exited(Int32, String)
        case timedOut(TimeInterval)
        /// Beacon's own words, such as why it refused a hand-off.
        case server(String)

        var errorDescription: String? {
            switch self {
            case let .exited(code, output):
                "Beacon exited with status \(code). \(output.suffix(300))"
            case let .server(message):
                message
            case let .timedOut(seconds):
                "Beacon didn't answer within \(Int(seconds)) seconds, so it was stopped."
            }
        }
    }

    static let defaultRepoPath = "~/Projects/beacon"

    private static var repoOverride: String? {
        UserDefaults.standard.string(forKey: repoKey).flatMap { $0.isEmpty ? nil : $0 }
    }

    /// The server on 127.0.0.1:8765 is started from the default checkout and reads its database.
    /// With any other checkout configured (a scratch copy, say) the app must not read from that
    /// server and write through this checkout's CLI: the two databases can hold different items
    /// under the same IDs. Reads then go through the CLI too, so reads and writes share one database.
    static func sharesServer(repoOverride: String?) -> Bool {
        guard let repoOverride else { return true }
        func resolved(_ path: String) -> String {
            URL(fileURLWithPath: NSString(string: path).expandingTildeInPath).resolvingSymlinksInPath().path
        }
        return resolved(repoOverride) == resolved(defaultRepoPath)
    }

    /// Whether the app is pointed at the same checkout the local server runs from.
    static var usesSharedServer: Bool { sharesServer(repoOverride: repoOverride) }

    private let repo = URL(fileURLWithPath: NSString(string: BeaconCLI.repoOverride ?? BeaconCLI.defaultRepoPath).expandingTildeInPath)

    func queue() async throws -> [Item] {
        if let items: [Item] = await fromServer("queue") { return items }
        return try JSONDecoder().decode([Item].self, from: run(["queue"]))
    }

    /// Reads from `beacon serve` on this Mac when it's running: about 0.1 s, where starting
    /// `beacon queue` took 0.5-2 s. Nil (so the CLI answers) when the server is down, or when a
    /// different checkout is configured and the server's database isn't the one writes go to.
    private func fromServer<T: Decodable>(_ path: String, timeout: TimeInterval = 5) async -> T? {
        guard Self.usesSharedServer else { return nil }
        let request = URLRequest(url: URL(string: "http://127.0.0.1:8765/\(path)")!, timeoutInterval: timeout)
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    func mark(_ status: String, ids: [Int]) throws {
        _ = try run(["mark", status] + ids.map(String.init))
    }

    func plans() async throws -> [Plan] {
        if let plans: [Plan] = await fromServer("briefs") { return plans }
        return try JSONDecoder().decode([Plan].self, from: run(["briefs"]))
    }

    func library(offset: Int, limit: Int, area: String?, search: LibrarySearch?) throws -> LibraryPage {
        let filter = (area.map { ["--area", $0] } ?? []) + (search?.arguments ?? [])
        return try JSONDecoder().decode(LibraryPage.self, from: run(["library", "--offset", String(offset), "--limit", String(limit)] + filter))
    }

    /// Outside the actor: a Haiku call takes seconds, and the grid keeps paging meanwhile.
    nonisolated func compose(search: String, ids: [Int]) async throws -> ComposeResult {
        let repo = repo
        return try await Task.detached {
            let args = ["compose", "layout", "--search=\(search)", "--ids", ids.map(String.init).joined(separator: ",")]
            return try JSONDecoder().decode(ComposeResult.self, from: Self.run(repo: repo, args))
        }.value
    }

    func composeChoice(id: Int, choice: String) throws {
        _ = try run(["compose", "choice", "--id", String(id), "--choice", choice])
    }

    func references(brief: String, ids: [Int], words: Bool) throws -> ReferenceReview {
        // "--brief=…" so a brief that starts with a dash stays a brief.
        let args = ["references", "--brief=\(brief)", "--ids", ids.map(String.init).joined(separator: ",")]
        return try JSONDecoder().decode(ReferenceReview.self, from: run(args + (words ? ["--allow-upload"] : [])))
    }

    func handled() async throws -> [HandledItem] {
        if let items: [HandledItem] = await fromServer("handled") { return items }
        return try JSONDecoder().decode([HandledItem].self, from: run(["handled"]))
    }

    func unhandle(id: Int) throws {
        _ = try run(["unhandle", String(id)])
    }

    func keep(url: String) throws {
        _ = try run(["keep", "--url", url])
    }

    func keep(picture: Data, width: Int, height: Int, note: String) throws {
        guard let suffix = pictureSuffix(picture) else { throw Failure.exited(1, "That isn't a picture Beacon can keep.") }
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + suffix)
        try picture.write(to: file)
        defer { try? FileManager.default.removeItem(at: file) }
        _ = try run(["keep", "--image", file.path, "--width", String(width), "--height", String(height)]
                    + (note.isEmpty ? [] : ["--note", note]))
    }

    func read(id: Int) throws {
        _ = try run(["read", String(id)])
    }

    /// Runs outside the actor so triage writes never wait behind it.
    nonisolated func plan(project: String) async throws {
        let repo = repo
        try await Task.detached {
            _ = try Self.run(repo: repo, ["brief", "--project", project, "--allow-upload"])
        }.value
    }

    func setBriefStatus(_ brief: Brief, to status: String) throws {
        _ = try run(["brief-status", String(brief.itemId), brief.workstream, status])
    }

    func finishBrief(_ brief: Brief, itemStatus: String) throws {
        _ = try run(["brief-done", String(brief.itemId), brief.workstream, "dispatched", itemStatus])
    }

    func setForMac(_ brief: Brief, _ on: Bool) throws {
        _ = try run(["brief-mac", String(brief.itemId), brief.workstream, on ? "on" : "off"])
    }

    // MARK: Activity

    /// Hand-offs and picks go to the server on this Mac when it serves this checkout: it runs each agent
    /// (or round build) in a thread of its own and answers at once, where `beacon activate` holds its answer
    /// until the run ends (up to 45 min). Nil when another checkout is configured; a server that's down is an
    /// error, not a silent CLI run.
    private static func toServer<T: Decodable>(_ path: String, _ body: [String: Any]) async throws -> T? {
        guard usesSharedServer else { return nil }
        var request = URLRequest(url: URL(string: "http://127.0.0.1:8765/\(path)")!, timeoutInterval: 30)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response): (Data, URLResponse)
        do { (data, response) = try await URLSession.shared.data(for: request) } catch {
            throw Failure.exited(1, "Beacon's server on this Mac isn't running. It restarts by itself; try again in a moment.")
        }
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            let message = (try? JSONDecoder().decode([String: String].self, from: data))?["error"]
            throw Failure.server(message ?? "Beacon's server returned an error.")
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    func activity() async throws -> [Activation] {
        if let records: [Activation] = await fromServer("activity") { return records }
        return try JSONDecoder().decode([Activation].self, from: run(["activity"]))
    }

    /// Through the local server when it serves this checkout; otherwise the CLI, which waits for the run,
    /// so this returns nil and Activity reads the new row from the ledger.
    nonisolated func activate(itemId: Int) async throws -> Activation? {
        if let record: Activation = try await Self.toServer("activate", ["item_id": itemId]) { return record }
        try await runWhileTheAgentWorks(["activate", String(itemId)])
        return nil
    }

    nonisolated func answer(activation id: Int, text: String) async throws -> Activation? {
        if let record: Activation = try await Self.toServer("activity/\(id)", ["answer": text]) { return record }
        // "--" so an answer that starts with a dash stays an answer.
        try await runWhileTheAgentWorks(["activity-answer", String(id), "--", text])
        return nil
    }

    func close(activation id: Int, as state: String) async throws -> Activation {
        if let record: Activation = try await Self.toServer("activity/\(id)", ["state": state]) { return record }
        return try await Self.inBeaconsWords { try JSONDecoder().decode(Activation.self, from: run(["activity-close", String(id), state])) }
    }

    // MARK: Pick

    func callSession(size: Int, new: Bool) async throws -> CallSession {
        // A long wait: giving up early and asking the CLI would store a second session.
        if let session: CallSession = await fromServer("calls/session?size=\(size)&new=\(new ? 1 : 0)", timeout: 60) {
            return session
        }
        return try await Self.inBeaconsWords {
            try JSONDecoder().decode(CallSession.self, from: run(["calls", "session", "--size", String(size)] + (new ? ["--new"] : [])))
        }
    }

    func checkCall(session: LooseID, card: LooseID, answer: String, destination: String?, chip: String?) async throws -> CallCheck {
        var body: [String: Any] = ["session_id": session.json, "card_id": card.json, "answer": answer]
        if let destination { body["destination"] = destination }
        if let chip { body["chip"] = chip }
        if let reply: CallCheck = try await Self.toServer("calls/check", body) { return reply }
        let args = ["calls", "check", "--session", session.description, "--card", card.description, "--answer", answer]
            + (destination.map { ["--destination=\($0)"] } ?? []) + (chip.map { ["--chip=\($0)"] } ?? [])
        return try await Self.inBeaconsWords { try JSONDecoder().decode(CallCheck.self, from: run(args)) }
    }

    func undoCall(change: LooseID) async throws {
        if let _: CallUndone = try await Self.toServer("calls/undo", ["change_id": change.json]) { return }
        _ = try await Self.inBeaconsWords { try run(["calls", "undo", "--change", change.description]) }
    }

    func callSummary(session: LooseID?) async throws -> CallSummary {
        let query = session.flatMap { "?session=\($0)".addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) } ?? ""
        if let summary: CallSummary = await fromServer("calls/summary\(query)") { return summary }
        return try await Self.inBeaconsWords {
            try JSONDecoder().decode(CallSummary.self, from: run(["calls", "summary"] + (session.map { ["--session", $0.description] } ?? [])))
        }
    }

    /// The CLI runs the whole re-decide before it answers; the server starts it and answers at once.
    nonisolated func redecide(group: String) async throws {
        if let _: RedecideStarted = try await Self.toServer("calls/redecide", ["group": group]) { return }
        try await runWhileTheAgentWorks(["calls", "redecide", "--group", group])
    }

    func pick(round id: Int, position: Int, choice: String, why: String?, shownAt: String?) async throws -> Int? {
        var body: [String: Any] = ["round_id": id, "position": position, "choice": choice]
        if let why { body["why"] = why }
        if let shownAt { body["shown_at"] = shownAt }
        if let reply: PickRecorded = try await Self.toServer("pick", body) { return reply.nextPosition }
        let args = ["pick", "record", "--round", String(id), "--position", String(position), "--choice", choice]
            + (why.map { ["--why=\($0)"] } ?? []) + (shownAt.map { ["--shown-at=\($0)"] } ?? [])
        return try await Self.inBeaconsWords { try JSONDecoder().decode(PickRecorded.self, from: run(args)).nextPosition }
    }

    func undoPick(round id: Int) async throws -> Int? {
        if let reply: PickUndone = try await Self.toServer("pick/undo", ["round_id": id]) { return reply.position }
        return try await Self.inBeaconsWords {
            try JSONDecoder().decode(PickUndone.self, from: run(["pick", "undo", "--round", String(id)])).position
        }
    }

    /// Outside the actor, like plan: the command lives as long as the agent's run.
    private nonisolated func runWhileTheAgentWorks(_ arguments: [String]) async throws {
        let repo = repo
        try await Self.inBeaconsWords { try await Task.detached { _ = try Self.run(repo: repo, arguments) }.value }
    }

    /// A refusal exits 2 with "command: why" on stderr, and `pick prepare` exits 1 with just the why;
    /// the app shows the why, not the exit status.
    static func inBeaconsWords<T>(_ work: () async throws -> T) async throws -> T {
        do { return try await work() } catch let Failure.exited(code, errors) where code == 1 || code == 2 {
            let line = errors.split(separator: "\n").last.map(String.init) ?? errors
            guard code == 2 else { throw Failure.server(line) }
            throw Failure.server(line.split(separator: ":", maxSplits: 1).last.map { $0.trimmingCharacters(in: .whitespaces) } ?? line)
        }
    }

    /// Outside the actor, like plan: triage keeps working during the few minutes this takes.
    nonisolated func sync() async throws {
        let repo = repo
        try await Task.detached {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/bin/zsh")
            process.arguments = ["scripts/weekly.sh"]
            process.currentDirectoryURL = repo
            try process.run()
            process.waitUntilExit()
            switch process.terminationStatus {
            case 0: return
            case 75: throw Failure.exited(75, "A sync is already running.")
            default:
                // The script logs to a file, not to us; its last line says what stopped it.
                let log = (try? String(contentsOf: repo.appending(path: "logs/weekly.log"), encoding: .utf8)) ?? ""
                throw Failure.exited(process.terminationStatus, log.split(separator: "\n").last.map(String.init) ?? "")
            }
        }.value
    }

    private func run(_ arguments: [String]) throws -> Data {
        try Self.run(repo: repo, arguments)
    }

    private static func run(repo: URL, _ arguments: [String]) throws -> Data {
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        return try run(URL(fileURLWithPath: "/usr/bin/env"), ["python3", "beacon.py"] + arguments,
                       in: repo, environment: environment, timeout: deadline(for: arguments))
    }

    /// The longest Python itself can wait during each command the app runs: the timeouts it can hit
    /// one after another. Change this with the timeout in lib/ it names.
    static let pythonWaits: [String: TimeInterval] = [
        // README fetches (30 s each), the planning agent (15 min, lib/briefs.py), then Jev's checks (20 s each).
        "brief": 20 * 60,
        // bird read (60 s) or a page fetch (15 s, lib/adds.py), then indexing the new save (30 min, lib/vectors.py).
        // The save is committed before indexing, so stopping early reports a save that landed as failed.
        "keep": 60 + 15 + 30 * 60,
        // A meaning search (120 s, lib/vectors.py), then the layout model (60 s, lib/compose.py).
        "compose": 120 + 60,
        // A meaning search, then Jev (20 s, lib/useful.py).
        "references": 120 + 20,
        // The agent's run (45 min, lib/activate.py RUN_TIMEOUT), after the git check (15 min, GIT_SCAN_TIMEOUT).
        "activate": 15 * 60 + 45 * 60,
        "activity-answer": 15 * 60 + 45 * 60,
        // `pick prepare`: EmbeddingGemma over the scored saves (30 min, lib/vectors.py); the other pick commands are quick.
        "pick": 30 * 60,
    ]

    /// Every other command is database work, a meaning search (120 s) or a Reader save (20 s).
    static let otherCommandsWait: TimeInterval = 120

    /// How long a command may run before the app stops it: Python's own limit plus a minute, so
    /// Python reports its own timeout first.
    static func deadline(for arguments: [String]) -> TimeInterval {
        (pythonWaits[arguments.first ?? ""] ?? otherCommandsWait) + 60
    }

    /// Runs a command and returns its output. Both pipes drain at once: reading stdout to the end
    /// first wedged the app when a command filled its stderr pipe while stdout stayed open.
    /// A command still running at `timeout` is stopped and throws `Failure.timedOut`.
    static func run(_ executable: URL, _ arguments: [String], in directory: URL? = nil,
                    environment: [String: String]? = nil, timeout: TimeInterval) throws -> Data {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.currentDirectoryURL = directory
        if let environment { process.environment = environment }
        let output = Pipe()
        let errors = Pipe()
        process.standardOutput = output
        process.standardError = errors
        let exited = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in exited.signal() }
        let drained = DispatchGroup()
        let out = Drain(output.fileHandleForReading, drained)
        let err = Drain(errors.fileHandleForReading, drained)
        do { try process.run() } catch {
            out.stop()
            err.stop()
            throw error
        }
        if exited.wait(timeout: .now() + timeout) == .timedOut {
            stop(process.processIdentifier, exited)
            out.stop()
            err.stop()
            throw Failure.timedOut(timeout)
        }
        // A child it started (the agent, the search helper) can hold the pipes open after it exits.
        _ = drained.wait(timeout: .now() + 5)
        let data = out.stop()
        let errorData = err.stop()
        guard process.terminationStatus == 0 else {
            throw Failure.exited(process.terminationStatus, String(decoding: errorData, as: UTF8.self))
        }
        return data
    }

    /// Stops a command that ran past its deadline, and everything it started. `Process` makes each
    /// command the leader of its own process group, so signalling the group also reaches the planning
    /// agent or search helper that Python started; stopping only Python left those running.
    /// The group gets SIGKILL after a grace period even if Python has exited, for a child that ignores SIGTERM.
    private static func stop(_ pid: pid_t, _ exited: DispatchSemaphore) {
        // Only signal a group this command leads: a wrong id here would stop the app's own group.
        let group: pid_t = pid > 1 && getpgid(pid) == pid ? -pid : pid
        kill(group, SIGTERM)
        _ = exited.wait(timeout: .now() + 2)
        kill(group, SIGKILL)
        _ = exited.wait(timeout: .now() + 2)
    }

    /// Collects everything a pipe receives on a background thread until the pipe closes or `stop()`.
    private final class Drain: @unchecked Sendable {
        private let handle: FileHandle
        private let lock = NSLock()
        private var data = Data()
        private var open = true
        private let group: DispatchGroup

        init(_ handle: FileHandle, _ group: DispatchGroup) {
            self.handle = handle
            self.group = group
            group.enter()
            handle.readabilityHandler = { [weak self] handle in
                let chunk = handle.availableData
                guard let self else { return }
                if chunk.isEmpty { self.finish() } else { self.lock.withLock { self.data.append(chunk) } }
            }
        }

        private func finish() {
            let first = lock.withLock { () -> Bool in
                defer { open = false }
                return open
            }
            guard first else { return }
            handle.readabilityHandler = nil
            group.leave()
        }

        /// Stops reading and returns what arrived.
        @discardableResult
        func stop() -> Data {
            finish()
            return lock.withLock { data }
        }
    }
}
