import Foundation
import Observation

/// An id the backend may send as a number or a string. It goes back the way it came.
enum LooseID: Codable, Hashable, Sendable, CustomStringConvertible {
    case number(Int)
    case text(String)

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if let number = try? value.decode(Int.self) { self = .number(number) } else { self = .text(try value.decode(String.self)) }
    }

    func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .number(let number): try value.encode(number)
        case .text(let text): try value.encode(text)
        }
    }

    var description: String {
        switch self {
        case .number(let number): String(number)
        case .text(let text): text
        }
    }

    /// For a JSON body built with JSONSerialization.
    var json: Any {
        switch self {
        case .number(let number): number
        case .text(let text): text
        }
    }
}

/// One Pick session, as `beacon calls session` and `GET /calls/session` print it (docs/plans/2026-11-19-pick-calls.md).
/// Cards carry no outcome, kind or group: Beacon's call stays hidden until you answer.
struct CallSession: Decodable, Equatable {
    struct Item: Decodable, Equatable {
        let id: Int
        let author: String?
        let text: String
        let url: String?
        let media: [String]

        enum CodingKeys: String, CodingKey { case id, author, text, url, media }

        init(id: Int, author: String?, text: String, url: String?, media: [String]) {
            (self.id, self.author, self.text, self.url, self.media) = (id, author, text, url, media)
        }

        /// `media` may list URLs or `{url, …}` objects.
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decode(Int.self, forKey: .id)
            author = try c.decodeIfPresent(String.self, forKey: .author)
            text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
            url = try c.decodeIfPresent(String.self, forKey: .url)
            if let urls = try? c.decodeIfPresent([String].self, forKey: .media) {
                media = urls
            } else {
                media = (try? c.decodeIfPresent([LibraryPage.Entry.Picture].self, forKey: .media))?.map(\.url) ?? []
            }
        }

        var link: URL? { url.flatMap(URL.init(string:)) }
        /// "/media/…" pictures come from `beacon serve`, like Library's.
        var picture: URL? { media.first.flatMap { URL(string: $0, relativeTo: LibraryPage.mediaBase)?.absoluteURL } }
    }

    struct Card: Decodable, Identifiable, Equatable {
        let cardId: LooseID
        let item: Item
        let position: Int
        /// Set when you answered this card before (an open session picked up again).
        var answer: String? = nil
        var id: LooseID { cardId }

        enum CodingKeys: String, CodingKey {
            case item, position, answer
            case cardId = "card_id"
        }
    }

    /// A pick-two pair, as `/pick/round` shows one, plus its own question.
    struct Pair: Decodable, Identifiable, Equatable {
        struct Side: Decodable, Equatable {
            let id: Int
            let source: String?
            let author: String?
            let text: String
            let url: String?
            let hasMedia: Bool?

            enum CodingKeys: String, CodingKey {
                case id, source, author, text, url
                case hasMedia = "has_media"
            }
        }

        let position: Int
        let kind: String
        let left: Side
        let right: Side
        /// left, right or none.
        let beaconGuess: String?
        let question: String?
        let roundId: Int?

        var id: Int { position }

        enum CodingKeys: String, CodingKey {
            case position, kind, left, right, question
            case beaconGuess = "beacon_guess"
            case roundId = "round_id"
        }
    }

    let sessionId: LooseID
    let cards: [Card]
    let pairs: [Pair]
    /// Reasons you can tap on a change.
    let chips: [String]
    /// The pick round the pairs belong to, when the pairs don't say.
    let roundId: Int?

    enum CodingKeys: String, CodingKey {
        case cards, pairs, chips
        case sessionId = "session_id"
        case roundId = "round_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try c.decode(LooseID.self, forKey: .sessionId)
        cards = try c.decode([Card].self, forKey: .cards).sorted { $0.position < $1.position }
        pairs = try c.decodeIfPresent([Pair].self, forKey: .pairs) ?? []
        chips = try c.decodeIfPresent([String].self, forKey: .chips) ?? []
        roundId = try c.decodeIfPresent(Int.self, forKey: .roundId)
    }
}

/// `beacon calls check` and `POST /calls/check`.
struct CallCheck: Decodable, Equatable {
    let agreed: Bool
    /// keep or pass.
    let beaconOutcome: String
    /// Why Beacon made its call, in second person: "Beacon passed this: …".
    let beaconReason: String?
    /// A project or area slug.
    let suggestedDestination: String?
    let changeId: LooseID?
    /// Reasons to tap, when the check offers its own instead of the session's.
    let chips: [String]?
    /// Other slugs a keep can go to, when the check offers them.
    let destinations: [String]?

    enum CodingKeys: String, CodingKey {
        case agreed, chips, destinations
        case beaconOutcome = "beacon_outcome"
        case beaconReason = "beacon_reason"
        case suggestedDestination = "suggested_destination"
        case changeId = "change_id"
    }
}

/// `beacon calls summary` and `GET /calls/summary`: the end-of-session portrait.
struct CallSummary: Decodable, Equatable {
    struct Session: Decodable, Equatable {
        let checked: Int
        let changed: Int
        let unsure: Int
    }

    struct Group: Decodable, Equatable {
        let name: String
        /// "Readable passes"; the name when there's no label.
        let label: String
        let checkedRandom: Int
        let changedRandom: Int
        /// The Wilson 90% interval, as shares (0.02 = 2%); nil before enough checks.
        let low: Double?
        let high: Double?
        let closed: Bool

        enum CodingKeys: String, CodingKey {
            case name, label, low, high, closed
            case checkedRandom = "checked_random"
            case changedRandom = "changed_random"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            name = try c.decode(String.self, forKey: .name)
            label = try c.decodeIfPresent(String.self, forKey: .label) ?? name
            checkedRandom = try c.decode(Int.self, forKey: .checkedRandom)
            changedRandom = try c.decode(Int.self, forKey: .changedRandom)
            low = try c.decodeIfPresent(Double.self, forKey: .low)
            high = try c.decodeIfPresent(Double.self, forKey: .high)
            closed = try c.decodeIfPresent(Bool.self, forKey: .closed) ?? false
        }
    }

    struct Axis: Decodable, Equatable {
        let question: String
        let decisive: Int
        /// Picks still needed before a marker shows.
        let needed: Int
        /// Where your picks lean, 0 to 1 toward the keep pole; nil before there's a lean.
        let lean: Double?
        let state: String?
        /// How many reasons you tapped on changes for this axis ("you said"), kept apart from what you chose.
        let said: Int

        enum CodingKeys: String, CodingKey { case question, decisive, needed, lean, state, said }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            question = try c.decode(String.self, forKey: .question)
            decisive = try c.decodeIfPresent(Int.self, forKey: .decisive) ?? 0
            needed = try c.decodeIfPresent(Int.self, forKey: .needed) ?? max(0, CallSummary.markerAt - decisive)
            lean = try c.decodeIfPresent(Double.self, forKey: .lean)
            state = try c.decodeIfPresent(String.self, forKey: .state)
            said = (try? c.decodeIfPresent(Int.self, forKey: .said)) ?? 0
        }

        var showsMarker: Bool { decisive >= CallSummary.markerAt && lean != nil }
        var settled: Bool { state == "confirmed" || state == "overturned" }
    }

    /// The axes the portrait shows: those with a decisive pick, plus the one the next pairs will ask about
    /// (fewest decisive picks among the unsettled, as lib/pick.py `next_axis` picks it). `later` counts the rest.
    var portraitAxes: (shown: [Axis], later: Int) {
        let open = axes.contains { !$0.settled } ? axes.filter { !$0.settled } : axes
        let next = open.min { $0.decisive < $1.decisive }
        let shown = axes.filter { $0.decisive > 0 || $0 == next }
        return (shown, axes.count - shown.count)
    }

    /// A group Beacon gets wrong often: Haiku can re-decide its calls with your checks as examples, once you approve.
    struct Offer: Decodable, Equatable {
        let group: String
        let label: String?
        let items: Int?
        let costUsd: Double
        enum CodingKeys: String, CodingKey { case group, label, items, costUsd = "cost_usd" }
    }

    /// An axis shows where you lean only after this many decisive picks (docs/pick.md).
    static let markerAt = 8
    /// A group's range shows only after this many random checks.
    static let rangeAt = 10

    let session: Session
    let groups: [Group]
    let flaggedNext: Int
    let axes: [Axis]
    let offer: Offer?

    enum CodingKeys: String, CodingKey {
        case session, groups, axes, offer
        case flaggedNext = "flagged_next"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        session = try c.decode(Session.self, forKey: .session)
        groups = try c.decodeIfPresent([Group].self, forKey: .groups) ?? []
        flaggedNext = try c.decodeIfPresent(Int.self, forKey: .flaggedNext) ?? 0
        axes = try c.decodeIfPresent([Axis].self, forKey: .axes) ?? []
        offer = try c.decodeIfPresent(Offer.self, forKey: .offer)
    }
}

/// `POST /pick` and `beacon pick record`.
struct PickRecorded: Decodable {
    let nextPosition: Int?
    enum CodingKeys: String, CodingKey { case nextPosition = "next_position" }
}

/// `POST /pick/undo` and `beacon pick undo`.
struct PickUndone: Decodable { let position: Int? }

/// `POST /calls/undo`.
struct CallUndone: Decodable { let restored: Bool }

/// `POST /calls/redecide`: any JSON object means it started.
struct RedecideStarted: Decodable {}

protocol PickBackend: Sendable {
    /// The open session, or a new one when none is open or `new` is set ("10 more").
    func callSession(size: Int, new: Bool) async throws -> CallSession
    /// Records your answer to a card, or replaces it when the card was already answered.
    func checkCall(session: LooseID, card: LooseID, answer: String, destination: String?, chip: String?) async throws -> CallCheck
    /// Puts a changed call back the way Beacon had it.
    func undoCall(change: LooseID) async throws
    func callSummary(session: LooseID?) async throws -> CallSummary
    /// Starts Haiku re-deciding one group's calls (it costs money: only from your tap on the offer).
    func redecide(group: String) async throws
    /// Records (or changes) one pair's pick; returns the next position without a pick, nil once the round is done.
    func pick(round id: Int, position: Int, choice: String, why: String?, shownAt: String?) async throws -> Int?
    /// Takes back the round's last pick; returns its position.
    func undoPick(round id: Int) async throws -> Int?
}

/// Pick: ten blind "Keep or pass?" cards that check Beacon's calls, two pairs, then a portrait of what
/// the session showed. The model waits for each check's answer before moving on, because the answer
/// decides whether the disagreement tray opens.
@MainActor
@Observable
final class PickModel {
    enum Answer: String { case keep, pass, unsure }
    enum PairChoice: String { case left, right, same }

    /// What's on screen.
    enum Step: Equatable {
        case loading
        case card(CallSession.Card)
        /// You and Beacon disagreed on this card.
        case tray(Tray)
        case pair(CallSession.Pair)
        /// Your pick is in; Beacon's guess shows for a moment.
        case reveal(Reveal)
        case end
        /// "Done for today".
        case resting
        case failed(String)
    }

    struct Tray: Equatable {
        let card: CallSession.Card
        let answer: Answer
        let result: CallCheck
        var chip: String?
        var destination: String?

        /// Beacon's call and your answer, in second person: "Beacon passed this. You'd keep it."
        var headline: String {
            let beacon = result.beaconOutcome == "keep" ? "Beacon kept this." : "Beacon passed this."
            let you = answer == .keep ? "You'd keep it." : "You'd pass."
            return "\(beacon) \(you)"
        }

        var chips: [String]

        /// Beacon's reason without its "Beacon passed this:" opening, which the headline already says.
        var reason: String? {
            guard var text = result.beaconReason?.trimmingCharacters(in: .whitespaces), !text.isEmpty else { return nil }
            for opening in ["Beacon passed this:", "Beacon kept this:"] where text.hasPrefix(opening) {
                text = String(text.dropFirst(opening.count)).trimmingCharacters(in: .whitespaces)
            }
            guard let first = text.first else { return nil }
            return first.uppercased() + text.dropFirst()
        }

        /// Slugs a keep can go to: the suggestion first. A pass goes nowhere.
        var destinations: [String] {
            guard answer == .keep else { return [] }
            var slugs = result.suggestedDestination.map { [$0] } ?? []
            for slug in result.destinations ?? [] where !slugs.contains(slug) { slugs.append(slug) }
            return slugs
        }
    }

    struct Reveal: Equatable {
        let pair: CallSession.Pair
        let choice: PairChoice
    }

    /// A change you made this session; undoable from the end screen.
    struct Change: Identifiable, Equatable {
        let changeId: LooseID
        let item: CallSession.Item
        let answer: Answer
        var undone = false
        var id: LooseID { changeId }
    }

    static let sessionSize = 10

    private(set) var session: CallSession?
    private(set) var step: Step = .loading
    private(set) var summary: CallSummary?
    private(set) var changes: [Change] = []
    /// You approved the summary's re-decide offer.
    private(set) var offerAccepted = false
    private(set) var toast: TriageStore.Toast?
    /// A check or pick is on its way; input waits for it.
    private(set) var busy = false
    /// Bumps on each agreement, for the tick.
    private(set) var agreements = 0
    /// The last answer, so the card can leave in its direction.
    private(set) var lastAnswer: Answer?

    private let backend: any PickBackend
    /// Answered steps, newest last, for Undo.
    private var history: [Done] = []
    /// Cards answered before this launch, in an open session picked up again; Undo doesn't reach them.
    private var answeredBefore: Set<LooseID> = []
    private var shownAt = Date()

    private enum Done {
        case card(CallSession.Card, Answer, LooseID?)
        case pair(CallSession.Pair, Int)
    }

    init(backend: any PickBackend) {
        self.backend = backend
    }

    var cardsDone: Int { answeredBefore.count + history.reduce(0) { if case .card = $1 { $0 + 1 } else { $0 } } }
    var cardsTotal: Int { session?.cards.count ?? 0 }
    var canUndo: Bool { !history.isEmpty && !busy }

    /// Picks up the open session once; switching tabs keeps your place.
    func load() async {
        if session != nil, !isFailed { return }
        await start(new: false)
    }

    private var isFailed: Bool { if case .failed = step { true } else { false } }

    /// "10 more" starts a new session; a first load picks up the open one.
    func start(new: Bool = true) async {
        step = .loading
        summary = nil
        changes = []
        history = []
        offerAccepted = false
        do {
            let fresh = try await backend.callSession(size: Self.sessionSize, new: new)
            session = fresh
            answeredBefore = Set(fresh.cards.filter { $0.answer != nil }.map(\.cardId))
            await advance()
        } catch {
            step = .failed(error.localizedDescription)
        }
    }

    func answer(_ answer: Answer) async {
        guard !busy, let session, case .card(let card) = step else { return }
        busy = true
        defer { busy = false }
        // Set before the wait, so the card already knows which way it will leave.
        lastAnswer = answer
        do {
            let result = try await backend.checkCall(session: session.sessionId, card: card.cardId,
                                                     answer: answer.rawValue, destination: nil, chip: nil)
            history.append(.card(card, answer, result.changeId))
            if let change = result.changeId { changes.append(Change(changeId: change, item: card.item, answer: answer)) }
            if answer == .unsure || result.agreed {
                if answer != .unsure { agreements += 1 }
                await advance()
            } else {
                step = .tray(Tray(card: card, answer: answer, result: result, chip: nil,
                                  destination: result.suggestedDestination, chips: result.chips ?? session.chips))
            }
        } catch {
            show("Couldn't save that: \(error.localizedDescription)", isError: true)
        }
    }

    func setChip(_ chip: String?) {
        guard case .tray(var tray) = step else { return }
        tray.chip = tray.chip == chip ? nil : chip
        step = .tray(tray)
    }

    func setDestination(_ destination: String) {
        guard case .tray(var tray) = step else { return }
        tray.destination = destination
        step = .tray(tray)
    }

    /// Closes the tray. A chip or another destination goes back to Beacon with the same answer.
    func continueFromTray() async {
        guard !busy, let session, case .tray(let tray) = step else { return }
        if tray.chip != nil || tray.destination != tray.result.suggestedDestination {
            busy = true
            defer { busy = false }
            do {
                let again = try await backend.checkCall(session: session.sessionId, card: tray.card.cardId, answer: tray.answer.rawValue,
                                                        destination: tray.answer == .keep ? tray.destination : nil, chip: tray.chip)
                if let change = again.changeId, case .card(let card, let answer, _) = history.last {
                    history[history.count - 1] = .card(card, answer, change)
                    if let at = changes.lastIndex(where: { $0.item.id == card.item.id }) {
                        changes[at] = Change(changeId: change, item: card.item, answer: answer)
                    }
                }
            } catch {
                show("Couldn't save that: \(error.localizedDescription)", isError: true)
                return
            }
        }
        await advance()
    }

    func pick(_ choice: PairChoice) async {
        guard !busy, let session, case .pair(let pair) = step else { return }
        guard let round = pair.roundId ?? session.roundId else {
            show("This pair has no round to save it in.", isError: true)
            return
        }
        busy = true
        defer { busy = false }
        let shown = ISO8601DateFormatter().string(from: shownAt)
        do {
            _ = try await backend.pick(round: round, position: pair.position, choice: choice.rawValue, why: nil, shownAt: shown)
            history.append(.pair(pair, round))
            step = .reveal(Reveal(pair: pair, choice: choice))
        } catch {
            show("Couldn't save that pick: \(error.localizedDescription)", isError: true)
        }
    }

    /// After the guess has shown.
    func finishReveal() async {
        guard case .reveal = step else { return }
        await advance()
    }

    /// Back one step: the last card or pair shows again, and a change goes back the way Beacon had it.
    func undo() async {
        guard !busy, let last = history.last else { return }
        busy = true
        defer { busy = false }
        do {
            switch last {
            case .card(let card, _, let change):
                if let change, changes.contains(where: { $0.changeId == change && !$0.undone }) {
                    try await backend.undoCall(change: change)
                }
                changes.removeAll { $0.item.id == card.item.id }
                history.removeLast()
                step = .card(card)
            case .pair(let pair, let round):
                _ = try await backend.undoPick(round: round)
                history.removeLast()
                step = .pair(pair)
            }
            lastAnswer = nil
            summary = nil
            shownAt = Date()
        } catch {
            show("Couldn't go back: \(error.localizedDescription)", isError: true)
        }
    }

    /// From the end screen: one change goes back the way Beacon had it.
    func undoChange(_ change: Change) async {
        guard !busy, let at = changes.firstIndex(where: { $0.changeId == change.changeId }), !changes[at].undone else { return }
        busy = true
        defer { busy = false }
        do {
            try await backend.undoCall(change: change.changeId)
            changes[at].undone = true
            await loadSummary()
        } catch {
            show("Couldn't undo that: \(error.localizedDescription)", isError: true)
        }
    }

    func doneForToday() { step = .resting }

    func acceptOffer() async {
        guard !busy, !offerAccepted, let offer = summary?.offer else { return }
        busy = true
        defer { busy = false }
        do {
            try await backend.redecide(group: offer.group)
            offerAccepted = true
        } catch {
            show("Couldn't start that: \(error.localizedDescription)", isError: true)
        }
    }

    private func advance() async {
        guard let session else { return }
        let answeredCards = answeredBefore.union(history.compactMap { if case .card(let card, _, _) = $0 { card.cardId } else { nil } })
        let pickedPairs = Set(history.compactMap { if case .pair(let pair, _) = $0 { pair.position } else { nil } })
        shownAt = Date()
        if let card = session.cards.first(where: { !answeredCards.contains($0.cardId) }) {
            step = .card(card)
        } else if let pair = session.pairs.first(where: { !pickedPairs.contains($0.position) }) {
            step = .pair(pair)
        } else {
            await loadSummary()
            step = .end
        }
    }

    private func loadSummary() async {
        guard let session else { return }
        do {
            summary = try await backend.callSummary(session: session.sessionId)
        } catch {
            show("Couldn't load the summary: \(error.localizedDescription)", isError: true)
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

extension PickModel.Answer {
    var label: String {
        switch self {
        case .keep: "Keep"
        case .pass: "Pass"
        case .unsure: "Can't tell"
        }
    }
}

extension CallSummary.Group {
    /// "30 checked · 2 changed · 2–16%", or how many more checks it needs.
    var line: String {
        let counts = "\(checkedRandom) checked · \(changedRandom) changed"
        guard checkedRandom >= CallSummary.rangeAt, let low, let high else {
            return "\(counts) · not enough checks yet"
        }
        return "\(counts) · \(Self.percent(low))–\(Self.percent(high))%" + (closed ? " · settled" : "")
    }

    static func percent(_ share: Double) -> String { String(Int((share * 100).rounded())) }
}
