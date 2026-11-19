import Foundation
import Observation

/// The current pick-two round, as `GET /pick/round` and `beacon pick round` print it (docs/pick.md).
/// With no round, only `reason` and `preparing` are set.
struct PickRound: Decodable, Equatable {
    struct Card: Decodable, Hashable {
        let id: Int
        let source: String
        let author: String?
        let text: String
        let takeaway: String?
        let url: String?
        let hasMedia: Bool

        enum CodingKeys: String, CodingKey {
            case id, source, author, text, takeaway, url
            case hasMedia = "has_media"
        }

        var link: URL? { url.flatMap(URL.init(string:)) }
    }

    struct Pair: Decodable, Identifiable, Equatable {
        let position: Int
        let kind: String
        let left: Card
        let right: Card
        var id: Int { position }
    }

    struct Picked: Decodable, Equatable {
        let position: Int
        let choice: String
        let why: String?
    }

    let roundId: Int?
    let reason: String?
    let preparing: Bool
    let axis: String?
    let question: String?
    let chips: [String]?
    let total: Int?
    let pairs: [Pair]?
    let picks: [Picked]?

    enum CodingKeys: String, CodingKey {
        case reason, preparing, axis, question, chips, total, pairs, picks
        case roundId = "round_id"
    }
}

/// Your lean per axis, from `GET /pick/summary`. For you to read; Beacon never sends it to a model.
struct PickSummary: Decodable {
    struct Axis: Decodable {
        let question: String
        let decisive: Int
        let towardKeep: Int
        /// inferred, leaning, leaning against, split, confirmed or overturned.
        let state: String
        let consistency: Double?

        enum CodingKeys: String, CodingKey {
            case question, decisive, state, consistency
            case towardKeep = "toward_keep"
        }
    }

    let picks: Int
    let axes: [String: Axis]
}

/// `POST /pick` and `beacon pick record`.
struct PickRecorded: Decodable {
    let nextPosition: Int?
    enum CodingKeys: String, CodingKey { case nextPosition = "next_position" }
}

/// `POST /pick/undo` and `beacon pick undo`.
struct PickUndone: Decodable { let position: Int? }

/// `POST /pick/prepare`.
struct PickPreparing: Decodable { let started: Bool }

protocol PickBackend: Sendable {
    func pickRound() async throws -> PickRound
    /// Records (or changes) one pick; returns the next position without a pick, nil once the round is done.
    func pick(round id: Int, position: Int, choice: String, why: String?, shownAt: String?) async throws -> Int?
    /// Takes back the round's last pick; returns its position.
    func undoPick(round id: Int) async throws -> Int?
    /// Starts building the next round (minutes); false when one is already being built.
    func preparePick(axis: String?) async throws -> Bool
    func pickSummary() async throws -> PickSummary
}

/// Pick: two of your own saves, which would you rather have saved, 20 times a round.
/// A tap moves on at once; the writes go out one after another behind it.
@MainActor
@Observable
final class PickModel {
    enum Choice: String { case left, right, neither, skip }

    private(set) var round: PickRound?
    /// Positions picked so far in this round.
    private(set) var picked: [Int] = []
    private(set) var summary: PickSummary?
    private(set) var toast: TriageStore.Toast?
    private(set) var isPreparing = false
    /// Picks sent but not yet answered.
    private(set) var pending = 0
    /// The chip you tapped for the pair on screen; sent with your pick, then cleared.
    var why: String?

    private let backend: any PickBackend
    /// How often to ask whether the next round is built; nil never polls (tests).
    private let poll: Duration?
    private var writes: Task<Void, Never>?
    /// When the pair on screen appeared, sent with its pick.
    private var shownAt = Date()

    init(backend: any PickBackend, poll: Duration? = .seconds(4)) {
        self.backend = backend
        self.poll = poll
    }

    var current: PickRound.Pair? {
        round?.pairs?.first { !picked.contains($0.position) }
    }

    var done: Int { picked.count }
    var total: Int { round?.pairs?.count ?? 0 }
    var isFinished: Bool { round?.roundId != nil && total > 0 && current == nil }

    func load() async {
        do {
            show(try await backend.pickRound())
            if isFinished { await loadSummary() }
            if isPreparing { await waitForRound() }
        } catch {
            toast(error.localizedDescription, isError: true)
        }
    }

    func choose(_ choice: Choice) {
        guard let roundId = round?.roundId, let pair = current else { return }
        let why = self.why
        let shown = ISO8601DateFormatter().string(from: shownAt)
        picked.append(pair.position)
        self.why = nil
        shownAt = Date()
        pending += 1
        let backend = backend
        let previous = writes
        writes = Task {
            await previous?.value
            do {
                _ = try await backend.pick(round: roundId, position: pair.position, choice: choice.rawValue, why: why, shownAt: shown)
            } catch {
                picked.removeAll { $0 >= pair.position }
                self.toast("Couldn't save that pick: \(error.localizedDescription)", isError: true)
            }
            pending -= 1
            if isFinished && pending == 0 { await loadSummary() }
        }
    }

    /// Back: the round's last pick is taken back and its pair shows again.
    func back() async {
        guard let roundId = round?.roundId, !picked.isEmpty else { return }
        await writes?.value
        do {
            if let position = try await backend.undoPick(round: roundId) {
                picked.removeAll { $0 >= position }
            }
            summary = nil
            why = nil
            shownAt = Date()
        } catch {
            toast("Couldn't go back: \(error.localizedDescription)", isError: true)
        }
    }

    /// Asks Beacon to build the next round, then checks every few seconds until it's ready.
    func prepare(axis: String? = nil) async {
        guard !isPreparing else { return }
        isPreparing = true
        do {
            _ = try await backend.preparePick(axis: axis)
        } catch {
            isPreparing = false
            toast("Couldn't start a round: \(error.localizedDescription)", isError: true)
            return
        }
        await waitForRound()
    }

    /// The Mac's CLI holds its reply until the round is built; the server answers at once and says `preparing`.
    private var waiting = false
    private func waitForRound() async {
        guard let poll, !waiting else {
            if self.poll == nil { isPreparing = false }
            return
        }
        waiting = true
        defer { waiting = false }
        while isPreparing {
            try? await Task.sleep(for: poll)
            guard let fresh = try? await backend.pickRound() else { continue }
            if !fresh.preparing { show(fresh) }
        }
    }

    private func show(_ fresh: PickRound) {
        if fresh.roundId != round?.roundId { summary = nil; why = nil }
        round = fresh
        picked = (fresh.picks ?? []).map(\.position)
        isPreparing = fresh.preparing
        shownAt = Date()
    }

    private func loadSummary() async {
        summary = try? await backend.pickSummary()
    }

    private func toast(_ message: String, isError: Bool = false) {
        let toast = TriageStore.Toast(message: message, isError: isError)
        self.toast = toast
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(isError ? 6 : 2.2))
            if self?.toast == toast { self?.toast = nil }
        }
    }
}
