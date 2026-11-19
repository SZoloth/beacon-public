import Foundation
import Observation

/// One hand-off of a save to its destination's agent, as `GET /activity` and `beacon activity` print it.
struct Activation: Codable, Identifiable, Hashable {
    enum State: String, Codable {
        case running, needsYou = "needs_you", readyToReview = "ready_to_review", done, didntWork = "didnt_work"
        /// dots or Muse: Beacon wrote the prompt and you send it yourself.
        case handedOff = "handed_off"
        /// A state this build doesn't know yet; shown with the finished ones rather than failing the list.
        case unknown

        init(from decoder: Decoder) throws {
            self = State(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
        }
    }

    /// What the run made or used: `folder`, `branch`, `file` or `url`.
    struct Link: Codable, Hashable {
        let kind: String
        let value: String
    }

    let id: Int
    let itemId: Int
    let title: String?
    let url: String?
    let destination: String
    let destinationName: String?
    let runner: String
    let state: State
    let summary: String?
    let needs: String?
    let links: [Link]
    let startedAt: String
    let updatedAt: String
    /// The prompt to send by hand, only while `handed_off`.
    let handoff: String?

    enum CodingKeys: String, CodingKey {
        case id, title, url, destination, runner, state, summary, needs, links, handoff
        case itemId = "item_id", destinationName = "destination_name", startedAt = "started_at", updatedAt = "updated_at"
    }

    var headline: String { title?.split(separator: "\n").first.map(String.init) ?? "Saved item" }
    var destinationLabel: String { destinationName ?? destination }
    var link: URL? { url.flatMap(URL.init(string:)) }

    /// "Codex", "Claude Code", "Muse" or "dots": who the save went to.
    var runnerName: String {
        switch runner {
        case "codex": "Codex"
        case "claude": "Claude Code"
        case "muse": "Muse"
        default: runner
        }
    }
}

/// The calls Activity makes; `Backend` has them too.
protocol ActivityBackend: Sendable {
    /// Every hand-off, newest first.
    func activity() async throws -> [Activation]
    /// Hands the save to its destination's agent. The run goes on in the background; nil when the
    /// backend can't return the record before the run ends (the Mac's CLI), so Activity reloads instead.
    func activate(itemId: Int) async throws -> Activation?
    /// Answers a hand-off that needs you; its run starts again.
    func answer(activation id: Int, text: String) async throws -> Activation?
    /// Closes a hand-off as "done" or "didnt_work". Only you close one.
    func close(activation id: Int, as state: String) async throws -> Activation
}

/// Activity: what each hand-off is doing, what needs you, and the buttons that start, answer and close them.
@MainActor
@Observable
final class ActivityModel {
    enum Kind: CaseIterable, Identifiable {
        case needsYou, readyToReview, running, done, didntWork
        var id: Self { self }

        var title: String {
            switch self {
            case .needsYou: "Needs you"
            case .readyToReview: "Ready to review"
            case .running: "Running"
            case .done: "Done"
            case .didntWork: "Didn't work"
            }
        }

        static func of(_ state: Activation.State) -> Kind {
            switch state {
            case .needsYou, .handedOff: .needsYou
            case .readyToReview: .readyToReview
            case .running: .running
            case .done: .done
            case .didntWork, .unknown: .didntWork
            }
        }
    }

    struct Section: Identifiable {
        let kind: Kind
        let records: [Activation]
        var id: Kind { kind }
    }

    /// Finished hand-offs shown per section; older ones stay in the ledger.
    static let finishedShown = 15

    private(set) var records: [Activation] = []
    private(set) var loadError: String?
    private(set) var toast: TriageStore.Toast?
    /// Saves whose start was sent and hasn't come back yet: their briefs leave the list at once.
    private(set) var starting: Set<Int> = []
    /// Hand-offs with an answer or close on its way, so their buttons don't take a second tap.
    private(set) var busy: Set<Int> = []

    private let backend: any ActivityBackend
    /// How often to read the ledger again while a run is going; nil never polls (tests).
    private let poll: Duration?
    private var polling: Task<Void, Never>?
    /// How long an answer waits for its reply before Activity reads the ledger instead. The server replies
    /// at once; the Mac's CLI holds its reply for the whole run, but a refusal still comes back in this time.
    private let answerGrace: Duration

    init(backend: any ActivityBackend, poll: Duration? = .seconds(5), answerGrace: Duration = .seconds(3)) {
        self.backend = backend
        self.poll = poll
        self.answerGrace = answerGrace
    }

    var sections: [Section] {
        let grouped = Dictionary(grouping: records) { Kind.of($0.state) }
        return Kind.allCases.compactMap { kind in
            guard var members = grouped[kind], !members.isEmpty else { return nil }
            if kind == .done || kind == .didntWork { members = Array(members.prefix(Self.finishedShown)) }
            return Section(kind: kind, records: members)
        }
    }

    /// What waits on you: a question, a hand-off to send, or work to look at. The tab's badge.
    var waitingCount: Int { records.filter { [.needsYou, .handedOff, .readyToReview].contains($0.state) }.count }

    /// The tab's badge and the page's count: plans with work still to start, plus hand-offs waiting on you.
    func toActOn(_ plans: [Plan]) -> Int {
        waitingCount + plans.flatMap(\.briefs).filter { !$0.nothingToDo && !isStarted(itemId: $0.itemId) }.count
    }

    /// Whether a save's brief has gone to an agent. One that didn't work can start again.
    func isStarted(itemId: Int) -> Bool {
        starting.contains(itemId) || records.contains { $0.itemId == itemId && $0.state != .didntWork }
    }

    func load() async {
        do {
            records = try await backend.activity()
            loadError = nil
        } catch {
            loadError = error.localizedDescription
        }
        keepPolling()
    }

    /// The only way a hand-off starts: your tap on a brief's button.
    func start(itemId: Int, destination: String) {
        guard !isStarted(itemId: itemId) else { return }
        starting.insert(itemId)
        show("Sent to \(destination)")
        let backend = backend
        Task {
            do {
                if let record = try await backend.activate(itemId: itemId) { upsert(record) }
                starting.remove(itemId)
                await load()
            } catch {
                starting.remove(itemId)
                show("Couldn't start: \(error.localizedDescription)", isError: true)
            }
        }
        // The Mac's CLI holds its answer until the run ends; the row is in the ledger well before.
        Task {
            try? await Task.sleep(for: .seconds(1.5))
            if starting.contains(itemId) { await load() }
        }
    }

    /// Sends your answer; false (and nothing sent) when it's blank or already on its way.
    @discardableResult
    func answer(_ record: Activation, _ text: String) async -> Bool {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.count <= 2000, !busy.contains(record.id) else { return false }
        busy.insert(record.id)
        defer { busy.remove(record.id) }
        let backend = backend
        let call = Task { try await backend.answer(activation: record.id, text: text) }
        let grace = answerGrace
        // The first of the reply and the grace period; the call itself goes on either way. (A task group
        // would wait for its children, and awaiting another task's result can't be cancelled.)
        let early: Result<Activation?, Error>? = await withCheckedContinuation { continuation in
            var resumed = false
            func finish(_ outcome: Result<Activation?, Error>?) {
                guard !resumed else { return }
                resumed = true
                continuation.resume(returning: outcome)
            }
            Task { @MainActor in finish(await call.result) }
            Task { @MainActor in try? await Task.sleep(for: grace); finish(nil) }
        }
        switch early {
        case .failure(let error):
            show("Couldn't send the answer: \(error.localizedDescription)", isError: true)
            return false
        case .success(let answered):
            if let answered { upsert(answered) }
        case nil:
            Task { [weak self] in
                if case .failure(let error) = await call.result {
                    self?.show("The answered run stopped: \(error.localizedDescription)", isError: true)
                }
            }
        }
        show("Answered · \(record.runnerName) picks it up again")
        await load()
        return true
    }

    func close(_ record: Activation, as state: Activation.State) async {
        guard state == .done || state == .didntWork, !busy.contains(record.id) else { return }
        busy.insert(record.id)
        defer { busy.remove(record.id) }
        do {
            upsert(try await backend.close(activation: record.id, as: state.rawValue))
            show(state == .done ? "Marked done" : "Marked didn't work")
        } catch {
            show("Couldn't close it: \(error.localizedDescription)", isError: true)
        }
    }

    private func upsert(_ record: Activation) {
        if let index = records.firstIndex(where: { $0.id == record.id }) {
            records[index] = record
        } else {
            records.insert(record, at: 0)
        }
        keepPolling()
    }

    /// Reads the ledger again every few seconds while anything runs, so states fill in by themselves.
    private func keepPolling() {
        guard let poll, polling == nil, records.contains(where: { $0.state == .running }) || !starting.isEmpty else { return }
        polling = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: poll)
                guard let self else { return }
                if let fresh = try? await self.backend.activity() { self.records = fresh }
                if !self.records.contains(where: { $0.state == .running }) && self.starting.isEmpty { break }
            }
            self?.polling = nil
        }
    }

    private func show(_ message: String, isError: Bool = false) {
        let toast = TriageStore.Toast(message: message, isError: isError)
        self.toast = toast
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(isError ? 6 : 2.2))
            if self?.toast == toast { self?.toast = nil }
        }
    }
}
