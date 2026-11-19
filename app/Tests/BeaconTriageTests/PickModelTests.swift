import Foundation
import Testing
@testable import BeaconTriage

private struct Down: Error {}

/// Answers like `beacon serve`'s pick routes: `GET /pick/round`, `POST /pick`, `/pick/undo`, `/pick/prepare`.
private actor FakePick: PickBackend {
    var round: PickRound
    var failing = false
    /// Rounds `pickRound` returns while a prepare runs, then the prepared one.
    var preparing: [PickRound] = []
    private(set) var recorded: [(Int, Int, String, String?)] = []
    private(set) var undos: [Int] = []
    private(set) var prepares: [String?] = []

    init(_ round: PickRound) { self.round = round }

    func pickRound() async throws -> PickRound {
        if !preparing.isEmpty { round = preparing.removeFirst() }
        return round
    }

    func pick(round id: Int, position: Int, choice: String, why: String?, shownAt: String?) async throws -> Int? {
        if failing { throw Down() }
        recorded.append((id, position, choice, why))
        let done = Set(recorded.map(\.1))
        return (round.pairs ?? []).map(\.position).first { !done.contains($0) }
    }

    func undoPick(round id: Int) async throws -> Int? {
        undos.append(id)
        guard let last = recorded.popLast() else { return nil }
        return last.1
    }

    func preparePick(axis: String?) async throws -> Bool {
        prepares.append(axis)
        return true
    }

    func pickSummary() async throws -> PickSummary {
        try JSONDecoder().decode(PickSummary.self, from: Data("""
        {"picks": 3, "axes": {"working": {"question": "Shows the working, or tells you about it?",
          "map_line": "Keep 1", "decisive": 3, "toward_keep": 2, "state": "inferred", "neither": 0, "skip": 0,
          "repeats": 0, "consistency": null, "foils": 0, "save_over_foil": 0, "top_why": [["real numbers", 1]]}}}
        """.utf8))
    }

    func setFailing(_ on: Bool) { failing = on }
    func setPreparing(_ rounds: [PickRound]) { preparing = rounds }
}

/// A round as `GET /pick/round` prints it: `count` pairs, the first `picked` already chosen.
private func round(id: Int = 5, count: Int = 3, picked: Int = 0) -> PickRound {
    func card(_ id: Int) -> String {
        """
        {"id": \(id), "source": "x", "author": "@a", "text": "Save \(id)", "takeaway": null,
         "url": "https://x.com/a/\(id)", "has_media": false}
        """
    }
    let pairs = (1...count).map { p in
        "{\"position\": \(p), \"kind\": \"matched\", \"beacon_guess\": \"left\", \"left\": \(card(p * 10)), \"right\": \(card(p * 10 + 1))}"
    }
    let picks = (0..<picked).map { "{\"position\": \($0 + 1), \"choice\": \"left\", \"why\": null}" }
    let json = """
    {"round_id": \(id), "axis": "working", "question": "Shows the working, or tells you about it?",
     "chips": ["shows the working", "real numbers", "less talk"], "total": \(count),
     "next_position": \(picked < count ? String(picked + 1) : "null"),
     "pairs": [\(pairs.joined(separator: ","))], "picks": [\(picks.joined(separator: ","))], "preparing": false}
    """
    return try! JSONDecoder().decode(PickRound.self, from: Data(json.utf8))
}

private let noRound = try! JSONDecoder().decode(PickRound.self, from: Data("""
{"round_id": null, "reason": "No round prepared. Run `beacon pick prepare`.", "preparing": false}
""".utf8))

private let building = try! JSONDecoder().decode(PickRound.self, from: Data("""
{"round_id": null, "reason": "Preparing the next round.", "preparing": true}
""".utf8))

@MainActor
private func until(_ condition: () -> Bool) async throws {
    for _ in 0..<500 where !condition() { try await Task.sleep(for: .milliseconds(10)) }
    try #require(condition())
}

@MainActor
struct PickModelTests {
    @Test func opensOnTheFirstPairNotYetPicked() async {
        let model = PickModel(backend: FakePick(round(count: 3, picked: 1)), poll: nil)
        await model.load()
        #expect(model.current?.position == 2)
        #expect(model.done == 1)
        #expect(model.round?.question == "Shows the working, or tells you about it?")
    }

    @Test func noRoundSaysWhy() async {
        let model = PickModel(backend: FakePick(noRound), poll: nil)
        await model.load()
        #expect(model.current == nil)
        #expect(model.round?.reason?.contains("No round prepared") == true)
    }

    @Test func aPickSendsThePositionAndChipThenMovesOn() async throws {
        let backend = FakePick(round(count: 3))
        let model = PickModel(backend: backend, poll: nil)
        await model.load()
        model.why = "real numbers"
        model.choose(.left)
        #expect(model.current?.position == 2)
        #expect(model.why == nil)
        try await until { model.pending == 0 }
        let sent = await backend.recorded
        #expect(sent.map(\.1) == [1])
        #expect(sent.first?.2 == "left")
        #expect(sent.first?.3 == "real numbers")
    }

    @Test func backTakesTheLastPickBack() async throws {
        let backend = FakePick(round(count: 3))
        let model = PickModel(backend: backend, poll: nil)
        await model.load()
        model.choose(.right)
        model.choose(.neither)
        #expect(model.current?.position == 3)
        await model.back()
        #expect(model.current?.position == 2)
        #expect(model.done == 1)
        #expect(await backend.undos == [5])
    }

    @Test func theLastPickFinishesTheRoundAndShowsTheLean() async throws {
        let model = PickModel(backend: FakePick(round(count: 2)), poll: nil)
        await model.load()
        model.choose(.left)
        model.choose(.skip)
        #expect(model.current == nil)
        #expect(model.isFinished)
        try await until { model.summary != nil }
        #expect(model.summary?.axes["working"]?.state == "inferred")
    }

    @Test func aFailedPickPutsThePairBackAndSaysSo() async throws {
        let backend = FakePick(round(count: 3))
        await backend.setFailing(true)
        let model = PickModel(backend: backend, poll: nil)
        await model.load()
        model.choose(.left)
        try await until { model.toast?.isError == true }
        #expect(model.current?.position == 1)
        #expect(model.done == 0)
    }

    @Test func nextRoundAsksForOneAndWaitsUntilItIsBuilt() async throws {
        let backend = FakePick(noRound)
        let model = PickModel(backend: backend, poll: .milliseconds(10))
        await model.load()
        await backend.setPreparing([building, building, round(id: 6, count: 2)])
        await model.prepare()
        #expect(await backend.prepares == [nil])
        try await until { model.round?.roundId == 6 }
        #expect(model.current?.position == 1)
        #expect(!model.isPreparing)
    }
}
