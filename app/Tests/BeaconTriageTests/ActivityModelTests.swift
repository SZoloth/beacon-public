import Foundation
import Testing
@testable import BeaconTriage

private struct Refused: LocalizedError { let errorDescription: String? }

/// Answers like `beacon serve`: `GET /activity`, `POST /activate`, `POST /activity/<id>`.
private actor FakeActivity: ActivityBackend {
    var records: [Activation]
    var refusal: String?
    private(set) var started: [Int] = []
    private(set) var answers: [(Int, String)] = []
    private(set) var closes: [(Int, String)] = []

    /// How long an answer takes to come back: the Mac's CLI holds it for the whole run.
    var answerDelay: Duration = .zero

    init(_ records: [Activation], refusal: String? = nil, answerDelay: Duration = .zero) {
        self.records = records
        self.refusal = refusal
        self.answerDelay = answerDelay
    }

    func activity() async throws -> [Activation] { records }

    func activate(itemId: Int) async throws -> Activation? {
        started.append(itemId)
        if let refusal { throw Refused(errorDescription: refusal) }
        let record = activation(id: 99, item: itemId, state: "running")
        records.insert(record, at: 0)
        return record
    }

    func answer(activation id: Int, text: String) async throws -> Activation? {
        answers.append((id, text))
        let record = replace(id, state: "running")
        try await Task.sleep(for: answerDelay)
        return record
    }

    func close(activation id: Int, as state: String) async throws -> Activation {
        closes.append((id, state))
        return replace(id, state: state)
    }

    func setRecords(_ new: [Activation]) { records = new }

    private func replace(_ id: Int, state: String) -> Activation {
        let index = records.firstIndex { $0.id == id }!
        let record = activation(id: id, item: records[index].itemId, state: state)
        records[index] = record
        return record
    }
}

/// A record as `GET /activity` prints it; nullable fields null unless given.
private func activation(id: Int, item: Int, state: String, needs: String? = nil, handoff: String? = nil) -> Activation {
    let json: [String: Any?] = [
        "id": id, "item_id": item, "title": "Save \(item)", "url": "https://x.com/a/\(item)", "destination": "travel",
        "destination_name": "Travel", "runner": "claude", "target": nil, "state": state, "summary": nil, "needs": needs,
        "links": [["kind": "folder", "value": "/tmp/drafts/\(id)"]], "thread_ref": nil,
        "started_at": "2026-11-19T10:00:00+00:00", "updated_at": "2026-11-19T10:0\(id % 10):00+00:00", "handoff": handoff,
    ]
    let data = try! JSONSerialization.data(withJSONObject: json.mapValues { $0 ?? NSNull() })
    return try! JSONDecoder().decode(Activation.self, from: data)
}

/// Waits up to 5 s for a condition that background work makes true.
@MainActor
private func until(_ condition: () -> Bool) async throws {
    for _ in 0..<500 where !condition() { try await Task.sleep(for: .milliseconds(10)) }
    try #require(condition())
}

@MainActor
struct ActivityModelTests {
    @Test func decodesARecordAsTheServerSendsIt() throws {
        let data = Data("""
        {"id": 3, "item_id": 41, "title": null, "url": null, "destination": "muse", "destination_name": null,
         "runner": "muse", "target": null, "state": "handed_off", "summary": "Prompt ready to hand to Muse",
         "needs": null, "links": [], "thread_ref": null, "started_at": "2026-11-19T10:00:00+00:00",
         "updated_at": "2026-11-19T10:01:00+00:00", "handoff": "Do the thing", "reused": false}
        """.utf8)
        let record = try JSONDecoder().decode(Activation.self, from: data)
        #expect(record.state == .handedOff)
        #expect(record.destinationLabel == "muse")
        #expect(record.headline == "Saved item")
        #expect(record.handoff == "Do the thing")
    }

    @Test func anUnknownStateDecodesInsteadOfFailingTheList() throws {
        let data = Data("""
        [{"id": 1, "item_id": 2, "title": "t", "url": null, "destination": "d", "destination_name": "D", "runner": "claude",
          "target": null, "state": "paused", "summary": null, "needs": null, "links": [], "thread_ref": null,
          "started_at": "x", "updated_at": "x", "handoff": null}]
        """.utf8)
        #expect(try JSONDecoder().decode([Activation].self, from: data).first?.state == .unknown)
    }

    @Test func sectionsPutWhatNeedsSamFirstAndSkipEmptyOnes() async {
        let backend = FakeActivity([
            activation(id: 1, item: 1, state: "done"),
            activation(id: 2, item: 2, state: "ready_to_review"),
            activation(id: 3, item: 3, state: "running"),
            activation(id: 4, item: 4, state: "needs_you", needs: "Which hotel?"),
            activation(id: 5, item: 5, state: "handed_off", handoff: "prompt"),
        ])
        let model = ActivityModel(backend: backend, poll: nil)
        await model.load()
        #expect(model.sections.map(\.kind) == [.needsYou, .readyToReview, .running, .done])
        #expect(model.sections.first?.records.map(\.id) == [4, 5])
        #expect(model.waitingCount == 3)
    }

    @Test func startingSendsTheItemAndHidesItsBriefAtOnce() async throws {
        let backend = FakeActivity([])
        let model = ActivityModel(backend: backend, poll: nil)
        await model.load()
        model.start(itemId: 41, destination: "Travel")
        #expect(model.isStarted(itemId: 41))
        try await until { model.records.contains { $0.itemId == 41 } }
        #expect(await backend.started == [41])
        #expect(model.toast?.isError == false)
    }

    @Test func aRefusalShowsWhyAndBringsTheBriefBack() async throws {
        let backend = FakeActivity([], refusal: "Folio has client folders, so Beacon never hands saves there.")
        let model = ActivityModel(backend: backend, poll: nil)
        model.start(itemId: 41, destination: "Folio")
        try await until { model.toast?.isError == true }
        #expect(model.toast?.message.contains("client folders") == true)
        #expect(!model.isStarted(itemId: 41))
    }

    @Test func aBriefWhoseHandOffDidntWorkCanStartAgain() async {
        let model = ActivityModel(backend: FakeActivity([activation(id: 1, item: 41, state: "didnt_work")]), poll: nil)
        await model.load()
        #expect(!model.isStarted(itemId: 41))
    }

    @Test func answeringSendsTheTextAndTheRecordRunsAgain() async throws {
        let backend = FakeActivity([activation(id: 4, item: 4, state: "needs_you", needs: "Which hotel?")])
        let model = ActivityModel(backend: backend, poll: nil)
        await model.load()
        let record = model.records[0]
        #expect(await model.answer(record, "  ") == false)
        #expect(await model.answer(record, " The Ritz. ") == true)
        #expect(await backend.answers.map(\.1) == ["The Ritz."])
        #expect(model.records.first?.state == .running)
    }

    @Test(.timeLimit(.minutes(1))) func anAnswerThatHoldsForTheWholeRunStillShowsTheRunGoing() async {
        let backend = FakeActivity([activation(id: 4, item: 4, state: "needs_you", needs: "Which hotel?")], answerDelay: .seconds(30))
        let model = ActivityModel(backend: backend, poll: nil, answerGrace: .milliseconds(50))
        await model.load()
        let started = ContinuousClock.now
        #expect(await model.answer(model.records[0], "The Ritz.") == true)
        #expect(ContinuousClock.now - started < .seconds(5))
        #expect(model.records.first?.state == .running)
        #expect(model.busy.isEmpty)
    }

    @Test func closingAsDoneOrDidntWorkUpdatesTheRecord() async {
        let backend = FakeActivity([activation(id: 2, item: 2, state: "ready_to_review"),
                                    activation(id: 5, item: 5, state: "handed_off")])
        let model = ActivityModel(backend: backend, poll: nil)
        await model.load()
        await model.close(model.records[0], as: .done)
        await model.close(model.records[1], as: .didntWork)
        #expect(await backend.closes.map(\.1) == ["done", "didnt_work"])
        #expect(model.records.map(\.state) == [.done, .didntWork])
        #expect(model.waitingCount == 0)
    }

    @Test func theBadgeCountsPlansNotYetStartedAndHandOffsWaitingOnSam() async throws {
        let plans = try JSONDecoder().decode([Plan].self, from: Data("""
        [{"workstream": "folio", "project_name": "Folio", "summary": "", "overlaps": [], "briefs": [
          {"item_id": 9, "workstream": "folio", "project_name": "Folio", "unsupported_claims": [],
           "brief": {"verdict": "do it", "summary": "", "why_here": "", "next_step": "", "steps": [], "files": [],
                     "effort": "", "risks": "", "task_prompt": ""}},
          {"item_id": 4, "workstream": "folio", "project_name": "Folio", "unsupported_claims": [],
           "brief": {"verdict": "do it", "summary": "", "why_here": "", "next_step": "", "steps": [], "files": [],
                     "effort": "", "risks": "", "task_prompt": ""}}]}]
        """.utf8))
        let model = ActivityModel(backend: FakeActivity([activation(id: 1, item: 4, state: "ready_to_review")]), poll: nil)
        await model.load()
        #expect(model.toActOn(plans) == 2)
    }

    @Test func pollingPicksUpARunThatFinished() async throws {
        let backend = FakeActivity([activation(id: 3, item: 3, state: "running")])
        let model = ActivityModel(backend: backend, poll: .milliseconds(20))
        await model.load()
        await backend.setRecords([activation(id: 3, item: 3, state: "ready_to_review")])
        try await until { model.records.first?.state == .readyToReview }
    }
}
