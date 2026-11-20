import Foundation
import Testing
@testable import BeaconTriage

private struct Down: Error {}

/// Answers like `beacon serve`'s calls routes and the pair routes it keeps (`/pick`, `/pick/undo`).
/// Beacon passed every card except those in `kept`.
private actor FakeCalls: PickBackend {
    var kept: Set<Int> = []
    var failing = false
    private(set) var sessions = 0
    private(set) var newSessions = 0
    /// Cards already answered in the open session.
    var answered: Set<Int> = []
    private(set) var checks: [(card: String, answer: String, destination: String?, chip: String?)] = []
    private(set) var undoneChanges: [String] = []
    private(set) var picks: [(round: Int, position: Int, choice: String)] = []
    private(set) var undonePicks: [Int] = []
    private(set) var summaries = 0
    private var nextChange = 100

    func callSession(size: Int, new: Bool) async throws -> CallSession {
        sessions += 1
        if new { newSessions += 1 }
        return try JSONDecoder().decode(CallSession.self, from: Data(sessionJSON(cards: 3, pairs: 2, answered: new ? [] : answered).utf8))
    }

    func checkCall(session: LooseID, card: LooseID, answer: String, destination: String?, chip: String?) async throws -> CallCheck {
        if failing { throw Down() }
        checks.append((card.description, answer, destination, chip))
        let item = Int(card.description.dropFirst())! // "c7" → 7
        let beacon = kept.contains(item) ? "keep" : "pass"
        let agreed = answer == "unsure" || answer == beacon
        var change = "null"
        if !agreed { nextChange += 1; change = String(nextChange) }
        return try JSONDecoder().decode(CallCheck.self, from: Data("""
        {"agreed": \(agreed), "beacon_outcome": "\(beacon)", "beacon_reason": "Beacon \(beacon == "pass" ? "passed" : "kept") this: you pass on tool lists.",
         "suggested_destination": \(beacon == "pass" ? "\"reading\"" : "null"), "change_id": \(change)}
        """.utf8))
    }

    func undoCall(change: LooseID) async throws { undoneChanges.append(change.description) }

    private(set) var redecided: [String] = []
    func redecide(group: String) async throws { redecided.append(group) }

    func callSummary(session: LooseID?) async throws -> CallSummary {
        summaries += 1
        return try JSONDecoder().decode(CallSummary.self, from: Data(summaryJSON.utf8))
    }

    func pick(round id: Int, position: Int, choice: String, why: String?, shownAt: String?) async throws -> Int? {
        picks.append((id, position, choice))
        return nil
    }

    func undoPick(round id: Int) async throws -> Int? {
        undonePicks.append(id)
        return picks.popLast()?.position
    }

    func keep(_ items: Set<Int>) { kept = items }
    func setAnswered(_ items: Set<Int>) { answered = items }
    func setFailing(_ on: Bool) { failing = on }
}

/// A session as `GET /calls/session` prints it: items 1…cards, then pairs on round 9.
private func sessionJSON(cards: Int, pairs: Int, answered: Set<Int> = []) -> String {
    let cardList = (1...cards).map { n in
        """
        {"card_id": "c\(n)", "position": \(n), "answer": \(answered.contains(n) ? "\"pass\"" : "null"), "item": {"id": \(n), "author": "@a", "text": "Save \(n)",
         "source": "x", "url": "https://x.com/a/\(n)", "media": [{"url": "/media/\(n).jpg", "width": 10, "height": 10}]}}
        """
    }
    func side(_ id: Int) -> String { "{\"id\": \(id), \"source\": \"x\", \"author\": \"@b\", \"text\": \"Pair save \(id)\", \"url\": null, \"has_media\": false}" }
    let pairList = (0..<pairs).map { p in
        """
        {"position": \(p + 1), "kind": "\(p == 0 ? "matched" : "repeat")", "beacon_guess": "left",
         "question": "Which shows the working?", "left": \(side(p * 2 + 50)), "right": \(side(p * 2 + 51))}
        """
    }
    return """
    {"session_id": "s42", "round_id": 9, "chips": ["real numbers", "I'd build it"], "cards": [\(cardList.joined(separator: ","))], "pairs": [\(pairList.joined(separator: ","))]}
    """
}

private let summaryJSON = """
{"session": {"checked": 3, "changed": 1, "unsure": 0},
 "groups": [{"name": "readable", "label": "Readable passes", "checked_random": 30, "changed_random": 2, "low": 0.0213, "high": 0.163, "closed": false},
            {"name": "Keeps", "checked_random": 4, "changed_random": 0, "low": null, "high": null, "closed": false}],
 "flagged_next": 8,
 "axes": [{"question": "Shows the working?", "decisive": 9, "needed": 0, "lean": 0.7, "state": "leaning", "said": 2},
          {"question": "Made by one person?", "decisive": 3, "needed": 5, "lean": null, "state": "inferred", "said": 0}],
 "offer": {"group": "readable", "label": "Readable passes", "items": 7065, "cost_usd": 1.41}}
"""

@MainActor
struct PickModelTests {
    @Test func opensOnTheFirstCardWithBeaconsCallHidden() async {
        let model = PickModel(backend: FakeCalls())
        await model.load()
        guard case .card(let card) = model.step else { Issue.record("expected a card"); return }
        #expect(card.item.id == 1)
        #expect(card.item.media == ["/media/1.jpg"])
        #expect(model.cardsTotal == 3)
        #expect(model.cardsDone == 0)
    }

    @Test func loadKeepsYourPlaceAcrossTabSwitches() async {
        let backend = FakeCalls()
        let model = PickModel(backend: backend)
        await model.load()
        await model.answer(.pass)
        await model.load()
        #expect(await backend.sessions == 1)
        #expect(model.cardsDone == 1)
    }

    @Test func anAgreementMovesStraightOn() async {
        let backend = FakeCalls()
        let model = PickModel(backend: backend)
        await model.load()
        await model.answer(.pass)
        guard case .card(let card) = model.step else { Issue.record("expected the next card"); return }
        #expect(card.item.id == 2)
        #expect(model.agreements == 1)
        #expect(model.changes.isEmpty)
    }

    @Test func aDisagreementOpensTheTrayInSecondPerson() async {
        let model = PickModel(backend: FakeCalls())
        await model.load()
        await model.answer(.keep)
        guard case .tray(let tray) = model.step else { Issue.record("expected the tray"); return }
        #expect(tray.headline == "Beacon passed this. You'd keep it.")
        #expect(tray.reason == "You pass on tool lists.")
        #expect(tray.destination == "reading")
        #expect(tray.destinations == ["reading"])
        #expect(tray.chips == ["real numbers", "I'd build it"])
        #expect(model.changes.count == 1)
    }

    @Test func passingAKeepNamesNoDestination() async {
        let backend = FakeCalls()
        await backend.keep([1])
        let model = PickModel(backend: backend)
        await model.load()
        await model.answer(.pass)
        guard case .tray(let tray) = model.step else { Issue.record("expected the tray"); return }
        #expect(tray.headline == "Beacon kept this. You'd pass.")
        #expect(tray.destinations.isEmpty)
    }

    @Test func aChipOrNewDestinationIsSentAgainWithTheSameAnswer() async {
        let backend = FakeCalls()
        let model = PickModel(backend: backend)
        await model.load()
        await model.answer(.keep)
        model.setChip("real numbers")
        model.setDestination("ideas")
        await model.continueFromTray()
        let checks = await backend.checks
        #expect(checks.count == 2)
        #expect(checks[1].card == "c1" && checks[1].answer == "keep")
        #expect(checks[1].destination == "ideas" && checks[1].chip == "real numbers")
        guard case .card(let card) = model.step else { Issue.record("expected the next card"); return }
        #expect(card.item.id == 2)
    }

    @Test func leavingTheTrayUntouchedSendsNothingMore() async {
        let backend = FakeCalls()
        let model = PickModel(backend: backend)
        await model.load()
        await model.answer(.keep)
        await model.continueFromTray()
        #expect(await backend.checks.count == 1)
    }

    @Test func canNotTellNeverOpensTheTray() async {
        let model = PickModel(backend: FakeCalls())
        await model.load()
        await model.answer(.unsure)
        guard case .card(let card) = model.step else { Issue.record("expected the next card"); return }
        #expect(card.item.id == 2)
        #expect(model.agreements == 0)
    }

    @Test func undoFromTheTrayRestoresTheCallAndShowsTheCardAgain() async {
        let backend = FakeCalls()
        let model = PickModel(backend: backend)
        await model.load()
        await model.answer(.keep)
        await model.undo()
        #expect(await backend.undoneChanges == ["101"])
        guard case .card(let card) = model.step else { Issue.record("expected the card again"); return }
        #expect(card.item.id == 1)
        #expect(model.changes.isEmpty)
        #expect(model.cardsDone == 0)
    }

    @Test func undoAfterAnAgreementStepsBackWithoutAnyChangeToRestore() async {
        let backend = FakeCalls()
        let model = PickModel(backend: backend)
        await model.load()
        await model.answer(.pass)
        await model.undo()
        #expect(await backend.undoneChanges.isEmpty)
        guard case .card(let card) = model.step else { Issue.record("expected the card again"); return }
        #expect(card.item.id == 1)
    }

    @Test func aFailedCheckStaysOnTheCard() async {
        let backend = FakeCalls()
        await backend.setFailing(true)
        let model = PickModel(backend: backend)
        await model.load()
        await model.answer(.keep)
        guard case .card(let card) = model.step else { Issue.record("expected the same card"); return }
        #expect(card.item.id == 1)
        #expect(model.toast?.isError == true)
        #expect(model.cardsDone == 0)
    }

    @Test func pairsFollowTheCardsThenRevealBeaconsGuess() async {
        let backend = FakeCalls()
        let model = PickModel(backend: backend)
        await model.load()
        for _ in 0..<3 { await model.answer(.pass) }
        guard case .pair(let pair) = model.step else { Issue.record("expected a pair"); return }
        #expect(pair.question == "Which shows the working?")
        await model.pick(.same)
        guard case .reveal(let reveal) = model.step else { Issue.record("expected the reveal"); return }
        #expect(reveal.choice == .same)
        #expect(reveal.pair.beaconGuess == "left")
        let picks = await backend.picks
        #expect(picks.first?.round == 9 && picks.first?.position == 1 && picks.first?.choice == "same")
        await model.finishReveal()
        guard case .pair(let second) = model.step else { Issue.record("expected the second pair"); return }
        #expect(second.position == 2)
    }

    @Test func undoOnAPairTakesBackThePick() async {
        let backend = FakeCalls()
        let model = PickModel(backend: backend)
        await model.load()
        for _ in 0..<3 { await model.answer(.pass) }
        await model.pick(.left)
        await model.undo()
        #expect(await backend.undonePicks == [9])
        guard case .pair(let pair) = model.step else { Issue.record("expected the pair again"); return }
        #expect(pair.position == 1)
    }

    @Test func theSessionEndsOnThePortrait() async throws {
        let backend = FakeCalls()
        let model = PickModel(backend: backend)
        await model.load()
        await model.answer(.keep)
        await model.continueFromTray()
        await model.answer(.pass)
        await model.answer(.pass)
        for _ in 0..<2 {
            await model.pick(.left)
            await model.finishReveal()
        }
        #expect(model.step == .end)
        let summary = try #require(model.summary)
        #expect(summary.session.checked == 3 && summary.flaggedNext == 8)
        #expect(summary.groups[0].line == "30 checked · 2 changed · 2–16%")
        #expect(summary.groups[1].line == "4 checked · 0 changed · not enough checks yet")
        #expect(summary.axes[0].showsMarker)
        #expect(!summary.axes[1].showsMarker && summary.axes[1].needed == 5)
        #expect(model.changes.count == 1)
    }

    @Test func aChangeStaysUndoableFromTheEnd() async throws {
        let backend = FakeCalls()
        let model = PickModel(backend: backend)
        await model.load()
        await model.answer(.keep)
        await model.continueFromTray()
        await model.answer(.pass)
        await model.answer(.pass)
        for _ in 0..<2 { await model.pick(.right); await model.finishReveal() }
        let change = try #require(model.changes.first)
        await model.undoChange(change)
        #expect(await backend.undoneChanges == ["101"])
        #expect(model.changes.first?.undone == true)
        #expect(await backend.summaries == 2)
        // A second tap does nothing.
        await model.undoChange(change)
        #expect(await backend.undoneChanges.count == 1)
    }

    @Test func theOfferStartsOnlyOnYourTapAndOnce() async throws {
        let backend = FakeCalls()
        let model = PickModel(backend: backend)
        await model.load()
        for _ in 0..<3 { await model.answer(.pass) }
        for _ in 0..<2 { await model.pick(.left); await model.finishReveal() }
        #expect(model.summary?.offer?.costUsd == 1.41)
        #expect(await backend.redecided.isEmpty)
        await model.acceptOffer()
        await model.acceptOffer()
        #expect(await backend.redecided == ["readable"])
        #expect(model.offerAccepted)
    }

    @Test func tenMoreStartsAFreshSessionAndDoneRests() async {
        let backend = FakeCalls()
        let model = PickModel(backend: backend)
        await model.load()
        model.doneForToday()
        #expect(model.step == .resting)
        await model.start()
        #expect(await backend.sessions == 2)
        #expect(await backend.newSessions == 1)
        guard case .card = model.step else { Issue.record("expected a card"); return }
    }

    @Test func anOpenSessionPicksUpAfterTheCardsYouAnswered() async {
        let backend = FakeCalls()
        await backend.setAnswered([1, 2])
        let model = PickModel(backend: backend)
        await model.load()
        guard case .card(let card) = model.step else { Issue.record("expected a card"); return }
        #expect(card.item.id == 3)
        #expect(model.cardsDone == 2)
        #expect(!model.canUndo)
    }

    @Test func thePortraitShowsStartedAxesAndTheNextOneOnly() throws {
        func axis(_ q: String, _ decisive: Int, _ state: String) -> String {
            "{\"question\": \"\(q)\", \"decisive\": \(decisive), \"needed\": \(max(0, 8 - decisive)), \"lean\": null, \"state\": \"\(state)\", \"said\": 0}"
        }
        let json = """
        {"session": {"checked": 0, "changed": 0, "unsure": 0}, "groups": [], "flagged_next": 0, "offer": null,
         "axes": [\(axis("A", 16, "confirmed")), \(axis("B", 5, "inferred")), \(axis("C", 0, "inferred")),
                  \(axis("D", 0, "inferred")), \(axis("E", 0, "inferred"))]}
        """
        let summary = try JSONDecoder().decode(CallSummary.self, from: Data(json.utf8))
        #expect(summary.portraitAxes.shown.map(\.question) == ["A", "B", "C"])
        #expect(summary.portraitAxes.later == 2)
    }

    @Test func aClosedGroupSaysSo() throws {
        let json = """
        {"name": "keeps", "label": "Keeps", "checked_random": 40, "changed_random": 0, "low": 0.0, "high": 0.04, "closed": true}
        """
        let group = try JSONDecoder().decode(CallSummary.Group.self, from: Data(json.utf8))
        #expect(group.line == "40 checked · 0 changed · 0–4% · settled")
    }
}
