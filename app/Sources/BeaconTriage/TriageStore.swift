import Foundation
import Observation

@MainActor
@Observable
final class TriageStore {
    struct Toast: Identifiable, Equatable {
        let id = UUID()
        let message: String
        let isError: Bool
    }

    enum Mode: String, CaseIterable { case triage = "Triage", briefs = "Briefs" }

    /// Wait this long after the last Do for a project, so a burst of Dos becomes one agent run.
    let planDelay: Duration
    /// Where items without a theme go, so every item belongs to exactly one stack.
    static let looseTheme = "Everything else"

    var mode: Mode = .triage
    var selectedTheme: String?
    var selectedItemID: Int?
    private(set) var plans: [Plan] = []
    /// Do items per project whose plan is waiting or running.
    private(set) var planning: [String: [Item]] = [:]
    private(set) var items: [Item] = []
    /// What autopilot passed or sent to Reader on its own, newest first.
    private(set) var handled: [HandledItem] = []
    private(set) var isLoading = true
    private(set) var isSyncing = false
    private(set) var loadError: String?
    private(set) var toast: Toast?

    private var decided: [Int: Decision] = [:]
    private var choices: [Int: Decision] = [:]
    /// Each entry is one Apply; it covers a whole stack.
    private var history: [[(item: Item, decision: Decision)]] = []
    private var pendingWrites: Task<Set<Int>, Never>?
    private var planTimers: [String: Task<Void, Never>] = [:]
    private let cli: any Backend
    /// The last queue and plans, saved after each load and shown at the next launch while Beacon
    /// answers (that took 1-2 s when the Mac was busy). Nil: don't save.
    private let snapshot: URL?
    var onQueueLoaded: (([Item]) async -> Void)?

    struct Snapshot: Codable {
        let items: [Item]
        let plans: [Plan]
    }

    nonisolated static let defaultSnapshot = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        .appendingPathComponent(Bundle.main.bundleIdentifier ?? "Beacon", isDirectory: true)
        .appendingPathComponent("triage.json")

    init(backend: any Backend, planDelay: Duration = .seconds(20), snapshot: URL? = TriageStore.defaultSnapshot) {
        cli = backend
        self.planDelay = planDelay
        self.snapshot = snapshot
    }

    // MARK: Stacks

    /// Undecided items by theme, biggest first; themeless items share one stack at the end.
    var stacks: [Stack] {
        let open = items.filter { decided[$0.id] == nil }
        return Dictionary(grouping: open) { $0.stack?.theme ?? Self.looseTheme }
            .map { theme, members in
                Stack(theme: theme, description: members.first?.stack?.description ?? "Saves that didn't fit a theme.",
                      items: members.sorted { ($0.stack?.rank ?? .max, $0.id) < ($1.stack?.rank ?? .max, $1.id) })
            }
            .sorted { a, b in
                if (a.theme == Self.looseTheme) != (b.theme == Self.looseTheme) { return b.theme == Self.looseTheme }
                return a.items.count == b.items.count ? a.theme < b.theme : a.items.count > b.items.count
            }
    }

    var currentStack: Stack? { stacks.first { $0.theme == selectedTheme } ?? stacks.first }
    var selectedItem: Item? { currentStack.flatMap { stack in stack.items.first { $0.id == selectedItemID } ?? stack.items.first } }
    var canUndo: Bool { !history.isEmpty }
    var totalRemaining: Int { open.count }

    func selectStack(_ theme: String?) {
        // The Mac opens on the first stack and post. The iPhone opens on the stack list: a selection there
        // pushes past it, and a nil selection must stay nil or tapping a stack won't register as a change.
        selectedTheme = theme
        selectedItemID = TriagePanes.highlightsSelection ? currentStack?.items.first?.id : nil
    }

    /// ↑ / ↓ through the current stack's rows.
    func moveSelection(by offset: Int) {
        guard let rows = currentStack?.items, let item = selectedItem, let index = rows.firstIndex(of: item) else { return }
        selectedItemID = rows[min(max(index + offset, 0), rows.count - 1)].id
    }

    /// What Apply will do to this row: your change if you made one, else Beacon's suggestion.
    func choice(for item: Item) -> Decision { choices[item.id] ?? item.suggested }

    /// Sets the selected row's choice and stays on it, so you see the change land; Next (Return) moves on.
    /// Nothing is saved until Apply.
    func choose(_ decision: Decision) {
        guard let item = selectedItem, choice(for: item) != decision else { return }
        choices[item.id] = decision
        Platform.tap()
    }

    var hasNext: Bool {
        guard let rows = currentStack?.items, let item = selectedItem else { return false }
        return item != rows.last
    }

    func counts(for stack: Stack) -> [Decision: Int] {
        Dictionary(grouping: stack.items, by: choice(for:)).mapValues(\.count)
    }

    /// "2 Do · 3 Read · 19 Reject"
    static func summary(_ counts: [Decision: Int], keeps: Bool = false) -> String {
        [Decision.doIt, .read, .reject].compactMap { kind in
            counts[kind].map { "\($0) \(kind == .doIt && keeps ? "Keep" : kind.title)" }
        }.joined(separator: " · ")
    }

    // MARK: Decisions

    /// Saves every row of the current stack with its choice (or rejects them all), then moves to the next stack.
    func apply(rejectingAll: Bool = false) {
        guard let stack = currentStack else { return }
        let batch = stack.items.map { (item: $0, decision: rejectingAll ? Decision.reject : choice(for: $0)) }
        let summary = rejectingAll ? "Rejected \(batch.count)" : Self.summary(counts(for: stack), keeps: stack.isArea)
        for entry in batch { decided[entry.item.id] = entry.decision }
        history.append(batch)
        Platform.tap()
        show("\(summary) · \(undoHint) undoes it")
        selectStack(TriagePanes.highlightsSelection ? stacks.first?.theme : nil)
        save(batch)
    }

    /// Saves one item's choice right away, as its own undoable step (the one-app Today tray).
    func decide(_ item: Item, _ decision: Decision, named title: String? = nil) {
        guard decided[item.id] == nil else { return }
        let batch = [(item: item, decision: decision)]
        decided[item.id] = decision
        history.append(batch)
        Platform.tap()
        show("\(title ?? decision.title(for: item)) · \(undoHint) undoes it")
        save(batch)
    }

    /// Undecided items, for views that list them flat instead of by stack.
    var open: [Item] { items.filter { decided[$0.id] == nil } }

    /// Today's picks: saves Beacon suggests doing, the best from each project or shelf first, then
    /// the next best. (Best fit alone gave five from one shelf: shelf matches score higher.)
    /// Autopilot handles the rest.
    static let pickCount = Item.pickCount
    var picks: [Item] {
        Item.picks(from: open)
    }

    /// Puts a save autopilot handled back in the queue, and tells Beacon it got that one wrong.
    func unhandle(_ entry: HandledItem) {
        handled.removeAll { $0.id == entry.id }
        let cli = cli
        Task {
            try? await cli.unhandle(id: entry.id)
            await load()
        }
    }

    /// One CLI call per decision: a 24-item stack saves in one write, not 24.
    private func save(_ batch: [(item: Item, decision: Decision)]) {
        for kind in Decision.allCases {
            let group = batch.filter { $0.decision == kind }.map(\.item)
            guard !group.isEmpty else { continue }
            guard kind == .doIt else { write(kind, ids: group.map(\.id)); continue }
            // Keep on an area save is final (adopted); Do on a project save is planned by its agent.
            let kept = group.filter(\.isArea), planned = group.filter { !$0.isArea }
            if !kept.isEmpty { write(kind, ids: kept.map(\.id), status: "adopted") }
            if !planned.isEmpty {
                let saved = write(kind, ids: planned.map(\.id))
                planned.forEach { schedulePlan(for: $0, after: saved) }
            }
        }
    }

    func undo() {
        guard let last = history.popLast() else { return }
        for entry in last {
            decided[entry.item.id] = nil
            if entry.decision == .doIt, let slug = entry.item.bestProject?.slug {
                planning[slug]?.removeAll { $0.id == entry.item.id }
                // Nothing left to plan: don't start an agent run for an undone Do.
                if planning[slug]?.isEmpty == true { planTimers[slug]?.cancel(); planning[slug] = nil }
            }
        }
        write(nil, ids: last.map(\.item.id))
        selectStack(last.first?.item.stack?.theme ?? Self.looseTheme)
        show(last.contains { $0.decision == .read } ? "Restored. Items already in Readwise Reader stay there." : "Restored")
    }

    func open(_ item: Item? = nil) {
        guard let url = (item ?? selectedItem)?.link else { return }
        Platform.open(url)
    }

    func load() async {
        isLoading = true
        if items.isEmpty, let snapshot, let data = try? Data(contentsOf: snapshot),
           let saved = try? JSONDecoder().decode(Snapshot.self, from: data) {
            items = saved.items  // last time's list, on screen at once; the fresh one replaces it below
            plans = saved.plans
        }
        // An Apply's writes finish first, or its items come back as queued and its Undo is lost.
        _ = await pendingWrites?.value
        // Choices made while Beacon answers (the saved list is already on screen) may reach it after
        // it read the queue, so they're kept, with their Undo, for items it still lists.
        let answeredFrom = history.count
        do {
            items = try await cli.queue()
            // A malformed plan must never block triage, and neither may the handled list.
            plans = (try? await cli.plans()) ?? []
            handled = (try? await cli.handled()) ?? handled
            // Keep unapplied picks and your place: a Sync reloads mid-triage.
            let ids = Set(items.map(\.id))
            history = history.dropFirst(min(answeredFrom, history.count))
                .map { $0.filter { ids.contains($0.item.id) } }
                .filter { !$0.isEmpty }
            decided = Dictionary(history.joined().map { ($0.item.id, $0.decision) }, uniquingKeysWith: { _, last in last })
            choices = choices.filter { ids.contains($0.key) }
            loadError = nil
            if let snapshot {
                let saved = Snapshot(items: items, plans: plans)
                Task.detached(priority: .utility) {
                    try? FileManager.default.createDirectory(at: snapshot.deletingLastPathComponent(), withIntermediateDirectories: true)
                    try? JSONEncoder().encode(saved).write(to: snapshot, options: .atomic)
                }
            }
            if selectedItem == nil || !TriagePanes.highlightsSelection {
                selectStack(TriagePanes.highlightsSelection ? stacks.first?.theme : nil)
            }
            await onQueueLoaded?(picks)
        } catch {
            loadError = error.localizedDescription
        }
        isLoading = false
    }

    /// Pulls in new bookmarks and stars now instead of waiting for Monday, then reloads.
    func sync() async {
        guard !isSyncing else { return }
        isSyncing = true
        let before = Set(items.map(\.id))
        do {
            try await cli.sync()
            await load()
            let added = items.filter { !before.contains($0.id) }.count
            show(added == 0 ? "Synced · nothing new to triage" : "Synced · \(added) new to triage")
        } catch {
            show("Sync failed: \(error.localizedDescription)", isError: true)
        }
        isSyncing = false
    }

    // MARK: Plans

    /// Briefs that need you: the badge and the Briefs header leave out plans with nothing to do.
    var briefCount: Int { plans.reduce(0) { $0 + $1.briefs.filter { !$0.nothingToDo }.count } }

    var planningRows: [(project: String, items: [Item])] {
        planning.filter { !$0.value.isEmpty }
            .map { (project: $0.value[0].bestProject?.name ?? $0.key, items: $0.value) }
            .sorted { $0.project < $1.project }
    }

    func loadPlans() async {
        do { plans = try await cli.plans() } catch { show("Couldn't load plans: \(error.localizedDescription)", isError: true) }
    }

    func dismiss(_ brief: Brief) {
        let place = removeBrief(brief)
        let cli = cli
        Task {
            do { try await cli.setBriefStatus(brief, to: "dismissed") } catch {
                putBack(brief, at: place, error: error, "Couldn't dismiss")
            }
        }
    }

    /// You acted on it: the brief is done and the item counts as used.
    func complete(_ brief: Brief) {
        let place = removeBrief(brief)
        show("Marked as used")
        let cli = cli
        Task {
            do { try await cli.finishBrief(brief, itemStatus: "adopted") } catch {
                putBack(brief, at: place, error: error, "Couldn't mark it used")
            }
        }
    }

    /// Opens a pre-filled thread in the project and moves the item to "tried" (in progress).
    func send(_ brief: Brief, to destination: Destination) {
        guard let url = destination.url(for: brief), Platform.open(url) else {
            show("Couldn't open \(destination.rawValue). Is the app installed?", isError: true)
            return
        }
        let place = removeBrief(brief)
        show("Opened in \(destination.rawValue). Press Enter there to start.")
        let cli = cli
        Task {
            do { try await cli.finishBrief(brief, itemStatus: "tried") } catch {
                putBack(brief, at: place, error: error, "Opened in \(destination.rawValue), but couldn't mark it started")
            }
        }
    }

    /// The phone can't open Claude Code or Codex; this puts the brief at the top of Briefs on the Mac.
    func toggleForMac(_ brief: Brief) {
        let on = !(brief.forMac ?? false)
        for p in plans.indices {
            if let b = plans[p].briefs.firstIndex(of: brief) { plans[p].briefs[b].forMac = on }
        }
        show(on ? "On your Mac's list, at the top of Briefs" : "Off your Mac's list")
        let cli = cli
        Task {
            do { try await cli.setForMac(brief, on) } catch {
                self.show("Couldn't save: \(error.localizedDescription)", isError: true)
            }
        }
    }

    func copyPrompt(_ brief: Brief) {
        guard let prompt = brief.brief?.taskPrompt else { return }
        Platform.copy(prompt)
        show("Task prompt copied")
    }

    /// Where a removed brief sat, so a failed save can put it back.
    struct BriefPlace {
        let plan: Plan
        let planIndex: Int
        let briefIndex: Int
    }

    @discardableResult
    private func removeBrief(_ brief: Brief) -> BriefPlace? {
        let place = plans.indices.lazy.compactMap { p in
            self.plans[p].briefs.firstIndex(of: brief).map { BriefPlace(plan: self.plans[p], planIndex: p, briefIndex: $0) }
        }.first
        for index in plans.indices { plans[index].briefs.removeAll { $0 == brief } }
        plans.removeAll { $0.briefs.isEmpty }
        return place
    }

    /// A brief's save failed: show it again where it was and say why.
    private func putBack(_ brief: Brief, at place: BriefPlace?, error: Error, _ what: String) {
        show("\(what): \(error.localizedDescription)", isError: true)
        guard let place, !plans.contains(where: { $0.briefs.contains(brief) }) else { return }
        if let p = plans.firstIndex(where: { $0.workstream == place.plan.workstream }) {
            plans[p].briefs.insert(brief, at: min(place.briefIndex, plans[p].briefs.count))
        } else {
            var plan = place.plan
            plan.briefs = [brief]
            plans.insert(plan, at: min(place.planIndex, plans.count))
        }
    }

    /// Debounced per project: every new Do restarts that project's timer, then one agent plans them all.
    private func schedulePlan(for item: Item, after saved: Task<Set<Int>, Never>) {
        guard let slug = item.bestProject?.slug else { return }
        planning[slug, default: []].append(item)
        planTimers[slug]?.cancel()
        let cli = cli
        let delay = planDelay
        planTimers[slug] = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled else { return }
            _ = await saved.value
            _ = await self?.pendingWrites?.value  // every Do in the burst is recorded first
            // A Do whose save failed was taken off the list; plan only if one that saved remains.
            guard self?.planning[slug]?.isEmpty == false else { self?.planning[slug] = nil; return }
            do {
                try await cli.plan(project: slug)
            } catch {
                self?.show("Planning failed for \(item.bestProject?.name ?? slug); the weekly run retries it.", isError: true)
            }
            self?.planning[slug] = nil
            await self?.loadPlans()
        }
    }

    // MARK: Plumbing

    /// Writes run one at a time in order, off the main thread; the rows are already gone.
    /// nil means "back to queued" (undo). The task returns the ids that didn't save; those rows
    /// come back before it returns, so a reload or a plan that waits on it sees them restored.
    @discardableResult
    private func write(_ decision: Decision?, ids: [Int], status: String? = nil) -> Task<Set<Int>, Never> {
        let previous = pendingWrites
        let cli = cli
        let task = Task { [weak self] () -> Set<Int> in
            _ = await previous?.value
            var failed: Set<Int> = []
            var firstError: Error?
            if decision == .read {
                // Each Read is its own call; one failure mustn't leave the rest unsent.
                for id in ids {
                    do { try await cli.read(id: id) } catch {
                        failed.insert(id)
                        firstError = firstError ?? error
                    }
                }
            } else {
                do { try await cli.mark(status ?? decision?.rawValue ?? "queued", ids: ids) } catch {
                    failed = Set(ids)
                    firstError = error
                }
            }
            if let firstError { self?.failedToSave(failed, decision, error: firstError) }
            return failed
        }
        pendingWrites = task
        return task
    }

    /// Puts rows whose save failed back in the queue (or, for a failed Undo, says so) with the error.
    private func failedToSave(_ ids: Set<Int>, _ decision: Decision?, error: Error) {
        guard let decision else {
            show("Couldn't undo: \(error.localizedDescription)", isError: true)
            return
        }
        for id in ids where decided[id] == decision { decided[id] = nil }
        history = history.map { $0.filter { !ids.contains($0.item.id) } }.filter { !$0.isEmpty }
        for slug in Array(planning.keys) {
            planning[slug]?.removeAll { ids.contains($0.id) }
            if planning[slug]?.isEmpty == true { planTimers[slug]?.cancel(); planning[slug] = nil }
        }
        let count = ids.count == 1 ? "1 save" : "\(ids.count) saves"
        show("Couldn't save \(count), back in the queue: \(error.localizedDescription)", isError: true)
    }

    private func show(_ message: String, isError: Bool = false) {
        let toast = Toast(message: message, isError: isError)
        self.toast = toast
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(isError ? 6 : 2.2))
            if self?.toast == toast { self?.toast = nil }
        }
    }
}
