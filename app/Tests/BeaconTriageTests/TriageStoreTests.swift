import Foundation
import Testing
@testable import BeaconTriage

/// Records what the store asks Beacon to do. Writes succeed unless they touch an id in `failing`,
/// or change a brief while `briefsFail` is set.
private actor FakeBackend: Backend {
    let items: [Item]
    let fails: Bool
    let delay: Duration
    let failing: Set<Int>
    let briefsFail: Bool
    var planList: [Plan] = []
    private(set) var planned: [String] = []
    private(set) var marks: [(String, [Int])] = []
    private(set) var reads: [Int] = []
    private(set) var finished: [(Int, String)] = []

    init(items: [Item], fails: Bool = false, delay: Duration = .zero, plans: [Plan] = [],
         failing: Set<Int> = [], briefsFail: Bool = false) {
        self.items = items
        self.planList = plans
        self.fails = fails
        self.delay = delay
        self.failing = failing
        self.briefsFail = briefsFail
    }

    func queue() async throws -> [Item] {
        try await Task.sleep(for: delay)
        if fails { throw URLError(.cannotConnectToHost) }
        return items
    }
    func plans() async throws -> [Plan] { planList }
    func mark(_ status: String, ids: [Int]) async throws {
        if !failing.isDisjoint(with: ids) { throw URLError(.cannotConnectToHost) }
        marks.append((status, ids))
    }
    func read(id: Int) async throws {
        reads.append(id)
        if failing.contains(id) { throw URLError(.timedOut) }
    }
    func plan(project: String) async throws { planned.append(project) }
    func setBriefStatus(_ brief: Brief, to status: String) async throws {
        if briefsFail { throw URLError(.cannotConnectToHost) }
    }
    func finishBrief(_ brief: Brief, itemStatus: String) async throws {
        if briefsFail { throw URLError(.cannotConnectToHost) }
        finished.append((brief.itemId, itemStatus))
    }
    func setForMac(_ brief: Brief, _ on: Bool) async throws {}
    func sync() async throws {}
    func library(offset: Int, limit: Int, area: String?, search: LibrarySearch?) async throws -> LibraryPage {
        LibraryPage(total: 0, nextOffset: nil, items: [])
    }
    func keep(url: String) async throws {}
    func keep(picture: Data, width: Int, height: Int, note: String) async throws {}
    func references(brief: String, ids: [Int], words: Bool) async throws -> ReferenceReview {
        ReferenceReview(wordsChecked: false, note: nil, saves: [])
    }
    func handled() async throws -> [HandledItem] { [] }
    func unhandle(id: Int) async throws {}
    func compose(search: String, ids: [Int]) async throws -> ComposeResult {
        ComposeResult(id: nil, layout: nil, why: "fake", costUsd: 0, cached: false)
    }
    func composeChoice(id: Int, choice: String) async throws {}
    func activity() async throws -> [Activation] { [] }
    func activate(itemId: Int) async throws -> Activation? { nil }
    func answer(activation id: Int, text: String) async throws -> Activation? { nil }
    func close(activation id: Int, as state: String) async throws -> Activation { throw URLError(.unsupportedURL) }
    func callSession(size: Int, new: Bool) async throws -> CallSession { throw URLError(.unsupportedURL) }
    func checkCall(session: LooseID, card: LooseID, answer: String, destination: String?, chip: String?) async throws -> CallCheck {
        throw URLError(.unsupportedURL)
    }
    func undoCall(change: LooseID) async throws {}
    func redecide(group: String) async throws {}
    func callSummary(session: LooseID?) async throws -> CallSummary { throw URLError(.unsupportedURL) }
    func pick(round id: Int, position: Int, choice: String, why: String?, shownAt: String?) async throws -> Int? { nil }
    func undoPick(round id: Int) async throws -> Int? { nil }
}

/// A queued save on a code project that Beacon suggests doing.
private let doItem = try! JSONDecoder().decode(Item.self, from: Data("""
{"id": 7, "source": "x", "title": "t", "body": "b", "url": "https://x.com/a/7", "author": "a",
 "created_at": null, "intent": null, "preview": null, "stack": null,
 "projects": [{"slug": "proj", "name": "Proj", "fit": 0.9, "kind": "project"}],
 "gist": {"takeaway": "t", "kind": "tool", "suggestion": "do", "reason": "r"}}
""".utf8))

/// A save Beacon suggests reading, in one stack so a single Apply covers all of them.
private func reading(_ id: Int) -> Item {
    try! JSONDecoder().decode(Item.self, from: Data("""
    {"id": \(id), "source": "x", "title": "t", "body": "b", "url": "https://x.com/a/\(id)", "author": "a",
     "created_at": null, "intent": null, "preview": null, "stack": null, "projects": [],
     "gist": {"takeaway": "t", "kind": "article", "suggestion": "read", "reason": "r"}}
    """.utf8))
}

/// One plan holding one brief for `doItem`.
private let onePlan = try! JSONDecoder().decode([Plan].self, from: Data("""
[{"workstream": "proj", "project_name": "Proj", "summary": "", "overlaps": [], "briefs": [
  {"item_id": 7, "workstream": "proj", "project_name": "Proj", "unsupported_claims": [],
   "brief": {"verdict": "do it", "summary": "", "why_here": "", "next_step": "", "steps": [],
             "files": [], "effort": "", "risks": "", "task_prompt": ""}}]}]
""".utf8))

/// Waits up to 5 s for a condition that background work makes true.
@MainActor
private func until(_ condition: () -> Bool) async throws {
    for _ in 0..<500 where !condition() { try await Task.sleep(for: .milliseconds(10)) }
    try #require(condition())
}

/// A save Beacon suggests doing, for `slug` at `fit`.
private func doing(_ id: Int, _ slug: String, fit: Double) -> Item {
    try! JSONDecoder().decode(Item.self, from: Data("""
    {"id": \(id), "source": "x", "title": "t", "body": "b", "url": "https://x.com/a/\(id)", "author": "a",
     "created_at": null, "intent": null, "preview": null, "stack": null,
     "projects": [{"slug": "\(slug)", "name": "\(slug)", "fit": \(fit), "kind": "project"}],
     "gist": {"takeaway": "t", "kind": "tool", "suggestion": "do", "reason": "r"}}
    """.utf8))
}

@MainActor
struct TriageStoreTests {
    @Test func briefsWithNothingToDoLeaveTheCount() async throws {
        func brief(_ id: Int, _ verdict: String) -> String {
            """
            {"item_id": \(id), "workstream": "web", "project_name": "Web", "unsupported_claims": [],
             "brief": {"verdict": "\(verdict)", "summary": "", "why_here": "", "next_step": "", "steps": [],
                       "files": [], "effort": "", "risks": "", "task_prompt": ""}}
            """
        }
        let json = "[{\"workstream\": \"web\", \"project_name\": \"Web\", \"summary\": \"\", \"overlaps\": [], \"briefs\": ["
            + [brief(1, "try it"), brief(2, "reference only"), brief(3, "skip"), brief(4, "do it")].joined(separator: ",") + "]}]"
        let plans = try JSONDecoder().decode([Plan].self, from: Data(json.utf8))
        let store = TriageStore(backend: FakeBackend(items: [], plans: plans), snapshot: nil)
        await store.loadPlans()
        #expect(store.briefCount == 2)
    }

    @Test func widgetSnapshotUsesTheSameFivePicksAsTriage() async {
        let queue = (1...5).map { doing($0, "ideas", fit: 0.95) } + [doing(6, "folio", fit: 0.7), doing(7, "beacon", fit: 0.6)]
        let store = TriageStore(backend: FakeBackend(items: queue), snapshot: nil)
        var published: [Int] = []
        store.onQueueLoaded = { published = $0.map(\.id) }
        await store.load()
        #expect(published == [1, 6, 7, 2, 3])
    }

    @Test func picksTakeTheBestFromEachDestinationBeforeASecondFromAny() async throws {
        // The server sends the queue best fit first; five shelf saves outscore two project saves.
        let queue = (1...5).map { doing($0, "ideas", fit: 0.95) } + [doing(6, "folio", fit: 0.7), doing(7, "beacon", fit: 0.6)]
        let store = TriageStore(backend: FakeBackend(items: queue), snapshot: nil)
        await store.load()
        #expect(store.picks.map(\.id) == [1, 6, 7, 2, 3])
    }

    @Test func aDoPlansItsProject() async throws {
        let backend = FakeBackend(items: [doItem])
        let store = TriageStore(backend: backend, planDelay: .milliseconds(20), snapshot: nil)
        await store.load()
        store.decide(doItem, .doIt)
        try await Task.sleep(for: .milliseconds(400))
        #expect(await backend.planned == ["proj"])
    }

    @Test func aStartShowsTheLastQueueWhileBeaconAnswers() async throws {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".json")
        defer { try? FileManager.default.removeItem(at: file) }
        let first = TriageStore(backend: FakeBackend(items: [doItem]), snapshot: file)
        await first.load()
        try await Task.sleep(for: .milliseconds(300))  // saved off the main thread
        // The next launch can't reach Beacon yet, and still shows the list from last time.
        let next = TriageStore(backend: FakeBackend(items: [], fails: true), snapshot: file)
        await next.load()
        #expect(next.items.map(\.id) == [7])
        #expect(next.loadError != nil)
    }

    @Test func aChoiceMadeWhileTheFreshListLoadsSticksAndCanBeUndone() async throws {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".json")
        defer { try? FileManager.default.removeItem(at: file) }
        await TriageStore(backend: FakeBackend(items: [doItem]), snapshot: file).load()
        try await until { FileManager.default.fileExists(atPath: file.path) }
        // Next launch: the saved list shows at once; Beacon answers slowly, and its answer was read
        // before the Pass reached it, so it still lists the item.
        let store = TriageStore(backend: FakeBackend(items: [doItem], delay: .milliseconds(300)), snapshot: file)
        let loading = Task { await store.load() }
        try await until { store.open.map(\.id) == [7] }
        store.decide(doItem, .reject)
        await loading.value
        #expect(store.open.map(\.id) == [])
        store.undo()
        #expect(store.open.map(\.id) == [7])
    }

    @Test func undoingTheOnlyDoCancelsTheAgentRun() async throws {
        let backend = FakeBackend(items: [doItem])
        let store = TriageStore(backend: backend, planDelay: .milliseconds(100), snapshot: nil)
        await store.load()
        store.decide(doItem, .doIt)
        store.undo()
        try await Task.sleep(for: .milliseconds(500))
        #expect(await backend.planned.isEmpty)
        // The save went back to the queue.
        #expect(await backend.marks.last?.0 == "queued")
        #expect(store.open.map(\.id) == [7])
    }

    // B06: a write that fails must not leave its items hidden as if it had saved.

    @Test func aReadBatchTriesEveryItemAndBringsBackOnlyTheOneThatFailed() async throws {
        let backend = FakeBackend(items: [reading(1), reading(2), reading(3)], failing: [2])
        let store = TriageStore(backend: backend, snapshot: nil)
        await store.load()
        store.apply()
        try await until { store.toast?.isError == true }
        #expect(await backend.reads == [1, 2, 3])
        #expect(store.open.map(\.id) == [2])
    }

    @Test func aDoWhoseSaveFailsComesBackAndIsNeverPlanned() async throws {
        let backend = FakeBackend(items: [doItem], failing: [7])
        let store = TriageStore(backend: backend, planDelay: .milliseconds(20), snapshot: nil)
        await store.load()
        store.decide(doItem, .doIt)
        try await until { store.toast?.isError == true }
        try await Task.sleep(for: .milliseconds(300))
        #expect(await backend.planned.isEmpty)
        #expect(store.open.map(\.id) == [7])
        #expect(store.planningRows.isEmpty)
    }

    @Test func aFailedCompletePutsTheBriefBack() async throws {
        let store = TriageStore(backend: FakeBackend(items: [], plans: onePlan, briefsFail: true), snapshot: nil)
        await store.loadPlans()
        store.complete(onePlan[0].briefs[0])
        try await until { store.toast?.isError == true }
        #expect(store.plans.flatMap(\.briefs).map(\.itemId) == [7])
    }

    @Test func aFailedDismissPutsTheBriefBack() async throws {
        let store = TriageStore(backend: FakeBackend(items: [], plans: onePlan, briefsFail: true), snapshot: nil)
        await store.loadPlans()
        store.dismiss(onePlan[0].briefs[0])
        try await until { store.toast?.isError == true }
        #expect(store.plans.flatMap(\.briefs).map(\.itemId) == [7])
    }

    @Test func completeSavesTheBriefAndTheItemInOneCall() async throws {
        let backend = FakeBackend(items: [], plans: onePlan)
        let store = TriageStore(backend: backend, snapshot: nil)
        await store.loadPlans()
        store.complete(onePlan[0].briefs[0])
        try await until { store.plans.isEmpty }
        try await Task.sleep(for: .milliseconds(100))
        #expect(await backend.finished.map(\.1) == ["adopted"])
        #expect(await backend.marks.isEmpty)
    }
}
