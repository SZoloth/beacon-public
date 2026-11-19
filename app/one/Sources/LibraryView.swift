import Observation
import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

/// Every save with a picture, newest first, read a page at a time from `beacon library`,
/// optionally one area (shelf) at a time, searched with the one box (plus follow-up steps),
/// or narrowed to pictures like one save.
@MainActor
@Observable
final class LibraryModel {
    /// Follow-up steps one search can take.
    static let maxSteps = 5
    /// How many of the strongest results a generated layout covers (Beacon's own cap).
    static let arrangeLimit = 40
    /// The title Beacon gives the leftovers of a layout (`compose.OTHER`).
    static let otherMatches = "Other matches"

    private(set) var entries: [LibraryPage.Entry] = []
    private(set) var error: String?
    private(set) var isLoading = false
    private(set) var shelf: String?
    private(set) var search: LibrarySearch?
    /// The save whose look "More like this" is matching, for the header.
    private(set) var similarTo: LibraryPage.Entry?
    /// What answered the current search ("words", "pictures", "meaning"), from its first page.
    private(set) var searched: [String]?
    /// How many saves the current follow-up steps set aside.
    private(set) var setAside = 0
    /// A layout Claude suggested for the current search, offered and not yet answered or closed.
    private(set) var offer: ComposeResult?
    /// Whether the offered layout is showing in place of the grid.
    private(set) var arranged = false
    private var offerAnswered = false
    /// The search (by generation) a layout request is out for, so one search asks once.
    private var composing: Int?
    /// The "Arrange" switch; set by the view from its stored setting.
    var arrangeEnabled = false
    private var nextOffset: Int? = 0
    private var generation = 0
    var linkedEntry: LibraryPage.Entry?
    private var linkGeneration = 0
    private let backend: any Backend

    init(backend: any Backend) { self.backend = backend }

    /// Resolve a widget save independently of the current shelf or search.
    func openSave(_ id: Int) async {
        linkGeneration += 1
        let mine = linkGeneration
        if let entry = entries.first(where: { $0.id == id }) { linkedEntry = entry; return }
        // Widgets only show saves from the newest 200, so three pages covers a save that slid down
        // since; a removed save would otherwise page through the whole library.
        var offset: Int? = 0
        do {
            for _ in 0..<3 {
                guard let current = offset else { break }
                let page = try await backend.library(offset: current, limit: 200, area: nil, search: nil)
                guard mine == linkGeneration else { return }
                if let entry = page.items.first(where: { $0.id == id }) { linkedEntry = entry; return }
                guard page.nextOffset != offset else { break }
                offset = page.nextOffset
            }
            error = "This save is no longer in your library."
        } catch { self.error = error.localizedDescription }
    }

    func show(shelf: String?) async { await show(shelf: shelf, search: search) }

    /// Keeps a link or picture, then shows everything so the new save is first.
    func keep(_ add: (any Backend) async throws -> Void) async throws {
        try await add(backend)
        entries = []
        await show(shelf: nil, search: nil)
    }

    /// What a reference review covers: the first 40 saves shown.
    var reviewable: [LibraryPage.Entry] { Array(entries.prefix(40)) }

    func review(brief: String, words: Bool) async throws -> ReferenceReview {
        try await backend.references(brief: brief, ids: reviewable.map(\.id), words: words)
    }

    func showSimilar(to entry: LibraryPage.Entry) async {
        similarTo = entry
        await show(shelf: nil, search: .similar(entry.id))
    }

    // MARK: The one search box and its steps

    /// The text in the search box, when the Library is showing a search.
    var query: String? { if case .search(let text, _) = search { text } else { nil } }
    /// The follow-up steps on the current search, in order.
    var steps: [String] { if case .search(_, let refine) = search { refine } else { [] } }
    var canNarrow: Bool { query != nil && steps.count < Self.maxSteps && !entries.isEmpty }

    /// Results that fit well (everything, outside a search).
    var strong: [LibraryPage.Entry] { query == nil ? entries : entries.filter { $0.fit != "maybe" } }
    /// Results found by meaning alone: "Might also fit", under the grid.
    var maybe: [LibraryPage.Entry] { query == nil ? [] : entries.filter { $0.fit == "maybe" } }

    /// True when only words answered: the Mac's meaning search (beacon-embed) isn't running.
    var wordsOnly: Bool {
        guard let searched else { return false }
        return !searched.contains("pictures") && !searched.contains("meaning")
    }

    /// Searches from the box: a new search starts over, without steps.
    func search(_ text: String) async {
        await show(shelf: shelf, search: text.isEmpty ? nil : .search(text, refine: []))
    }

    /// Adds a follow-up step ("wooden") to the current search.
    func narrow(_ step: String) async {
        // beacon serve refuses a leading dash (the text reaches the helper's arguments).
        let step = String(step.trimmingCharacters(in: .whitespaces).drop { $0 == "-" })
        guard let query, !step.isEmpty, steps.count < Self.maxSteps else { return }
        await show(shelf: shelf, search: .search(query, refine: steps + [step]))
    }

    /// Goes back to the search with only its first `count` steps.
    func back(to count: Int) async {
        guard let query, count < steps.count else { return }
        await show(shelf: shelf, search: .search(query, refine: Array(steps.prefix(count))))
    }

    func show(shelf: String?, search: LibrarySearch?) async {
        guard shelf != self.shelf || search != self.search || entries.isEmpty else { return }
        if case .similar = search {} else { similarTo = nil }
        answerOffer("ignored")
        withAnimation(.spring(duration: 0.4)) { offer = nil; arranged = false }
        offerAnswered = false
        self.shelf = shelf
        self.search = search
        error = nil
        searched = nil
        setAside = 0
        generation += 1
        entries = []
        nextOffset = 0
        isLoading = false
        await loadMore()
        await requestLayout()
    }

    /// The morph between the grid and the arranged sections (Suave's: a spring with no bounce).
    static let morph = Animation.spring(duration: 0.4, bounce: 0)

    /// Reads pages until about 60 more saves arrive. Outside a search the feed also carries GitHub
    /// stars, which have no picture and stay out of the grid; a search keeps them, as text cards.
    func loadMore() async {
        guard !isLoading, let start = nextOffset else { return }
        isLoading = true
        let mine = generation
        defer { if mine == generation { isLoading = false } }
        var offset: Int? = start
        var added = 0
        let searching = query != nil
        do {
            while let current = offset, added < 60 {
                let page = try await backend.library(offset: current, limit: 200, area: shelf, search: search)
                guard mine == generation else { return }  // you switched shelves meanwhile
                // Pictures, plus links you added yourself even when the page has no preview picture.
                // Search pages are ranked afresh on each request, so a save can come back; keep the first.
                let have = Set(entries.map(\.id))
                let shown = page.items.filter { (searching || $0.picture != nil || $0.source == "add") && !have.contains($0.id) }
                if entries.isEmpty {
                    Bench.firstPage()
                    searched = page.searched
                    setAside = page.setAside ?? 0
                    withAnimation(.spring(duration: 0.5)) { entries = shown }
                } else {
                    entries += shown  // lands below the screen; animating it re-laid out the whole grid
                }
                added += shown.count
                offset = page.nextOffset
            }
            nextOffset = offset
            error = nil
            if shelf == nil, search == nil {
                let loaded = entries
                Task { await WidgetPublisher.library(loaded) }
            }
        } catch {
            // Typing on cancels the previous search; that's not a connection problem.
            if mine == generation, !Task.isCancelled { self.error = error.localizedDescription }
        }
    }

    // MARK: Generated layout (the "Arrange" switch)

    /// Asks for a layout of the strongest results once they've settled. Offered, never applied:
    /// the grid stays until you tap the offer. Says nothing when there's no layout.
    func requestLayout() async {
        guard arrangeEnabled, offer == nil, composing != generation, let query else { return }
        let ids = strong.prefix(Self.arrangeLimit).map(\.id)
        guard ids.count >= 4 else { return }  // Beacon needs two groups of two
        let mine = generation
        composing = mine
        defer { if composing == mine { composing = nil } }
        let text = ([query] + steps).joined(separator: ", then ")
        guard let result = try? await backend.compose(search: text, ids: Array(ids)),
              mine == generation, arrangeEnabled, result.layout != nil else { return }
        offerAnswered = false
        withAnimation(.spring(duration: 0.35, bounce: 0)) { offer = result }
    }

    /// Shows the offered layout in place of the grid.
    func arrange() {
        guard offer?.layout != nil else { return }
        answerOffer("kept")
        arranged = true
    }

    /// Closes the offer: "No thanks".
    func dismissLayout() {
        answerOffer("dismissed")
        offer = nil
        arranged = false
    }

    /// Back to the plain grid, keeping the offer (and its follow-ups) to arrange again.
    func showGrid() { arranged = false }

    /// The Arrange switch went off: an unanswered offer counts as ignored.
    func stopArranging() {
        answerOffer("ignored")
        offer = nil
        arranged = false
    }

    /// One answer per layout: the first of kept, dismissed or ignored.
    private func answerOffer(_ choice: String) {
        guard let id = offer?.id, !offerAnswered else { return }
        offerAnswered = true
        let backend = backend
        Task { try? await backend.composeChoice(id: id, choice: choice) }
    }

    /// The arranged sections: the pick, the titled groups, and "Other matches" last, which also takes
    /// strong results past the first 40.
    var sections: (pick: (entry: LibraryPage.Entry, why: String)?, groups: [(title: String, entries: [LibraryPage.Entry])]) {
        guard let layout = offer?.layout else { return (nil, []) }
        let byId = Dictionary(strong.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        var used = Set<Int>()
        let pick = layout.pick.flatMap { pick in byId[pick.id].map { (entry: $0, why: pick.why) } }
        if let pick { used.insert(pick.entry.id) }
        var groups: [(title: String, entries: [LibraryPage.Entry])] = []
        for group in layout.groups {
            let members = group.ids.compactMap { used.insert($0).inserted ? byId[$0] : nil }
            if !members.isEmpty { groups.append((group.title, members)) }
        }
        let rest = strong.filter { !used.contains($0.id) }
        if !rest.isEmpty {
            if let last = groups.indices.last, groups[last].title == Self.otherMatches {
                groups[last].entries += rest
            } else {
                groups.append((Self.otherMatches, rest))
            }
        }
        return (pick, groups)
    }

    /// The line above the title: the shelf, or what you're searching for.
    var subtitle: String {
        switch search {
        case .search(let text, _): "Saves for “\(text)”"
        case .words(let text): "Saves with “\(text)”"
        case .meaning(let text): "Saves about “\(text)”"
        case .similar: "Looks like “\(similarTo?.caption.prefix(40) ?? "this save")”"
        case nil: shelf.flatMap { slug in Theme.areas.first { $0.slug == slug }?.name } ?? "Everything"
        }
    }

    var emptyMessage: String {
        switch search {
        case .search(let text, let refine):
            refine.isEmpty ? "Nothing for “\(text)” yet. Try fewer or different words."
                : "The last step set everything aside. Tap an earlier step to go back."
        case .words(let text): "No saves with “\(text)”. Try fewer words."
        case .meaning: "Nothing close in meaning yet. Pictures are still being read; try again in a few minutes."
        case .similar: "No similar pictures yet. This one may not be read yet."
        case nil: "Nothing on this shelf yet. Keep a save in Triage and it lands here."
        }
    }
}

/// The calm half of the app: what you saved, by shelf. Triage is the yellow half where you decide.
struct LibraryView: View {
    let model: LibraryModel
    @State private var searchText = ""
    @AppStorage("libraryArrange") private var arrange = false
    @State private var adding = false
    @State private var reviewing = false
    /// The save open in its sheet; one sheet for the whole grid instead of one per tile.
    @State private var opened: LibraryPage.Entry?
    @State private var dropping = false
    @FocusState private var searchFocused: Bool
    @Namespace private var tiles

    var body: some View {
        #if os(macOS)
        HStack(spacing: 0) {
            ShelfList(model: model)
                .frame(width: 220)
                .background(Theme.shelfSide)
            grid(columns: 4)
        }
        #else
        grid(columns: 2)
        #endif
    }

    private func grid(columns: Int) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 8) {
                        Text(model.subtitle)
                            .font(.system(size: 15, weight: .medium))
                            .foregroundStyle(Theme.quiet)
                            .contentTransition(.opacity)
                            .lineLimit(1)
                        if case .similar = model.search {
                            Button("Show everything") { Task { await model.show(shelf: nil, search: nil) } }
                                .buttonStyle(.plain)
                                .font(.system(size: 13, weight: .semibold))
                                .foregroundStyle(Theme.accent)
                        }
                    }
                    HStack(alignment: .center) {
                        Text("Library")
                            .font(.system(size: TodayView.titleSize, weight: .heavy))
                            .kerning(-1.2)
                            .foregroundStyle(Theme.ink)
                        Spacer()
                        Button { reviewing = true } label: {
                            #if os(iOS)
                            Image(systemName: "checklist")
                                .font(.system(size: 15, weight: .semibold))
                                .foregroundStyle(Theme.ink)
                                .frame(width: 38, height: 38)
                                .background(Theme.shelfSide, in: .circle)
                            #else
                            Label("Review", systemImage: "checklist")
                                .font(.system(size: 15, weight: .semibold))
                                .foregroundStyle(Theme.ink)
                                .padding(.horizontal, 16)
                                .frame(height: 38)
                                .background(Theme.shelfSide, in: .capsule)
                            #endif
                        }
                        .buttonStyle(PressScale())
                        .disabled(model.entries.isEmpty)
                        .keyboardShortcut("r", modifiers: [.command, .shift])
                        .accessibilityLabel("Review references")
                        Button { adding = true } label: {
                            Label("Add", systemImage: "plus")
                                .font(.system(size: 15, weight: .semibold))
                                .foregroundStyle(.white)
                                .padding(.horizontal, 16)
                                .frame(height: 38)
                                .background(Theme.ink, in: .capsule)
                        }
                        .buttonStyle(PressScale())
                        .keyboardShortcut("n")
                    }
                }
                .padding(.top, TodayView.headerTop + 20)
                HStack(spacing: 10) {
                    SearchField(text: $searchText, focused: $searchFocused)
                    ArrangeSwitch(on: $arrange)
                }
                #if os(iOS)
                ShelfChips(model: model)
                #endif
                if model.query != nil {
                    SearchSteps(model: model)
                }
                if let error = model.error, model.entries.isEmpty {
                    Text(error).foregroundStyle(Theme.quiet)
                } else if !model.isLoading && model.entries.isEmpty {
                    Text(model.emptyMessage).foregroundStyle(Theme.quiet).padding(.top, 30)
                }
                if model.arranged {
                    ArrangedSections(model: model, columns: columns, namespace: tiles) { opened = $0 }
                        .transition(.opacity)
                } else {
                    Masonry(entries: model.strong, columns: columns, namespace: tiles) {
                        Task { await model.loadMore() }
                    } open: { opened = $0 }
                }
                if !model.maybe.isEmpty {
                    SectionTitle(title: "Might also fit", count: model.maybe.count)
                        .padding(.top, 12)
                    Masonry(entries: model.maybe, columns: columns) {
                        Task { await model.loadMore() }
                    } open: { opened = $0 }
                }
                if model.isLoading { ProgressView().frame(maxWidth: .infinity).padding() }
            }
            .padding(.horizontal, TodayView.gutter)
            .padding(.bottom, 40)
            .background { if Bench.enabled { BenchScrollDriver() } }
        }
        .background(Theme.shelf)
        .sheet(isPresented: $adding) { AddSheet(model: model) }
        .sheet(isPresented: $reviewing) { ReferenceSheet(model: model) }
        .sheet(item: $opened) { entry in
            SaveDetail(entry: entry) {
                searchText = ""
                Task { await model.showSimilar(to: entry) }
            }
        }
        #if os(macOS)
        // Drop pictures from Finder or a browser straight onto the Library.
        .dropDestination(for: Data.self) { items, _ in
            let pictures = items.filter { pictureSuffix($0) != nil }
            guard !pictures.isEmpty else { return false }
            Task { for picture in pictures { try? await model.keep { try await $0.keep(picture: picture, width: picture.pixelSize.width, height: picture.pixelSize.height, note: "") } } }
            return true
        } isTargeted: { dropping = $0 }
        .overlay {
            if dropping {
                RoundedRectangle(cornerRadius: 24, style: .continuous)
                    .strokeBorder(Theme.accent, style: StrokeStyle(lineWidth: 3, dash: [10, 8]))
                    .padding(12)
                    .allowsHitTesting(false)
            }
        }
        #endif
        .animation(.spring(duration: 0.35), value: model.shelf)
        .animation(.spring(duration: 0.35), value: model.search)
        .task { if model.entries.isEmpty { await model.loadMore() } }
        .onChange(of: model.linkedEntry, initial: true) {
            if let entry = model.linkedEntry { opened = entry; model.linkedEntry = nil }
        }
        .onChange(of: arrange, initial: true) {
            model.arrangeEnabled = arrange
            if arrange {
                Task { await model.requestLayout() }
            } else if model.offer != nil {
                withAnimation(LibraryModel.morph) { model.stopArranging() }
            }
        }
        // Searches a beat after you stop typing, not on every key.
        .task(id: searchText) {
            try? await Task.sleep(for: .milliseconds(400))
            guard !Task.isCancelled else { return }
            let text = searchText.trimmingCharacters(in: .whitespaces)
            if text.isEmpty, case .similar = model.search { return }  // "More like this" cleared the field
            // Already showing it, maybe with steps; a search cut off before its first page runs again.
            if text == model.query, !model.entries.isEmpty || model.isLoading || !model.steps.isEmpty { return }
            await model.search(text)
        }
        #if os(macOS)
        .background { Button("Find") { searchFocused = true }.keyboardShortcut("f").hidden() }
        #endif
    }
}

/// The one search box: words in the post and closeness in meaning (what's in the pictures, too), together.
private struct SearchField: View {
    @Binding var text: String
    var focused: FocusState<Bool>.Binding

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass").foregroundStyle(Theme.quiet)
            TextField("Search your saves", text: $text)
                .textFieldStyle(.plain)
                .focused(focused)
                .submitLabel(.search)
            if !text.isEmpty {
                Button { text = "" } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(Theme.quiet)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clear search")
                .transition(.scale.combined(with: .opacity))
            }
        }
        .font(.system(size: 15))
        .padding(.horizontal, 14)
        .frame(height: 40)
        .frame(maxWidth: 520)
        .background(Theme.shelfSide, in: .capsule)
        .animation(.spring(duration: 0.25), value: text.isEmpty)
    }
}

/// Whether a search offers to arrange its results into groups (a Claude call per search, about $0.002).
private struct ArrangeSwitch: View {
    @Binding var on: Bool

    var body: some View {
        Button {
            withAnimation(.spring(duration: 0.3, bounce: 0.2)) { on.toggle() }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "square.grid.3x1.below.line.grid.1x2")
                    .symbolEffect(.bounce, value: on)
                Text("Arrange")
            }
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(on ? .white : Theme.quiet)
            .padding(.horizontal, 12)
            .frame(height: 34)
            .background(on ? Theme.ink : Theme.shelfSide, in: .capsule)
            .contentShape(.capsule)
        }
        .buttonStyle(PressScale())
        .accessibilityAddTraits(on ? .isSelected : [])
        .help("Offer to group search results")
    }
}

/// Under the search box: the trail of steps, a field for the next one, what the steps set aside,
/// the layout offer, and a quiet line when meaning search is off.
private struct SearchSteps: View {
    let model: LibraryModel
    @State private var step = ""
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if model.wordsOnly {
                Text("Searching words only: meaning search isn't running on this Mac.")
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.quiet)
                    .transition(.opacity)
            }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    if !model.steps.isEmpty, let query = model.query {
                        chip(query, at: 0)
                        ForEach(Array(model.steps.enumerated()), id: \.offset) { index, text in
                            Image(systemName: "chevron.right")
                                .font(.system(size: 10, weight: .bold))
                                .foregroundStyle(Theme.quiet)
                            chip(text, at: index + 1)
                        }
                    }
                    if model.canNarrow {
                        narrowField
                    }
                    if model.setAside > 0 {
                        Text("\(model.setAside) set aside")
                            .font(.system(size: 13))
                            .foregroundStyle(Theme.quiet)
                            .contentTransition(.numericText())
                            .padding(.leading, 4)
                    }
                }
                .animation(.spring(duration: 0.35, bounce: 0.2), value: model.steps)
            }
            .scrollClipDisabled()
            if let offer = model.offer, !model.arranged {
                LayoutOffer(model: model, offer: offer)
                    .transition(.asymmetric(insertion: .move(edge: .top).combined(with: .opacity),
                                            removal: .scale(scale: 0.96).combined(with: .opacity)))
            }
            if let next = model.offer?.layout?.next, !next.isEmpty, model.canNarrow {
                HStack(spacing: 6) {
                    ForEach(next, id: \.self) { text in
                        Button { Task { await model.narrow(text) } } label: {
                            Label(text, systemImage: "plus")
                                .font(.system(size: 13, weight: .medium))
                                .foregroundStyle(Theme.ink)
                                .padding(.horizontal, 12)
                                .frame(height: 30)
                                .overlay(Capsule().strokeBorder(Theme.shelfSide, lineWidth: 1.5))
                        }
                        .buttonStyle(PressScale())
                    }
                }
                .transition(.opacity.combined(with: .offset(y: -4)))
            }
        }
        .animation(.spring(duration: 0.4), value: model.offer)
        .animation(.spring(duration: 0.4), value: model.arranged)
    }

    /// One step of the trail: tap to go back to it; the last one has an x that removes it.
    private func chip(_ text: String, at count: Int) -> some View {
        let last = count == model.steps.count
        return HStack(spacing: 6) {
            Button { Task { await model.back(to: count) } } label: {
                Text(text).lineLimit(1)
            }
            .buttonStyle(.plain)
            .disabled(last)
            if last && count > 0 {
                Button { Task { await model.back(to: count - 1) } } label: {
                    Image(systemName: "xmark").font(.system(size: 9, weight: .bold))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Remove “\(text)”")
            }
        }
        .font(.system(size: 13, weight: last ? .semibold : .medium))
        .foregroundStyle(last ? .white : Theme.ink)
        .padding(.horizontal, 12)
        .frame(height: 30)
        .background(last ? Theme.ink : Theme.shelfSide, in: .capsule)
        .transition(.scale(scale: 0.8).combined(with: .opacity))
    }

    private var narrowField: some View {
        HStack(spacing: 4) {
            Image(systemName: "plus").font(.system(size: 11, weight: .bold))
            TextField("Narrow…", text: $step)
                .textFieldStyle(.plain)
                .focused($focused)
                .submitLabel(.search)
                .frame(width: focused || !step.isEmpty ? 140 : 64)
                .onSubmit {
                    let text = step
                    step = ""
                    Task { await model.narrow(text) }
                }
        }
        .font(.system(size: 13))
        .foregroundStyle(Theme.quiet)
        .padding(.horizontal, 12)
        .frame(height: 30)
        .overlay(Capsule().strokeBorder(Theme.shelfSide, lineWidth: 1.5))
        .animation(.spring(duration: 0.3), value: focused)
    }
}

/// The tray that offers a generated layout. The grid stays put until you tap it.
private struct LayoutOffer: View {
    let model: LibraryModel
    let offer: ComposeResult

    var body: some View {
        let count = offer.layout?.groups.filter { $0.title != LibraryModel.otherMatches }.count ?? 0
        HStack(spacing: 10) {
            Button {
                withAnimation(LibraryModel.morph) { model.arrange() }
                Platform.tap()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "sparkle").foregroundStyle(Theme.accent)
                    Text("Arrange into \(count) groups").foregroundStyle(Theme.ink)
                }
                .font(.system(size: 14, weight: .semibold))
            }
            .buttonStyle(PressScale())
            Button {
                withAnimation(.spring(duration: 0.4)) { model.dismissLayout() }
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Theme.quiet)
                    .frame(width: 22, height: 22)
                    .background(Theme.shelf, in: .circle)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("No thanks")
        }
        .padding(.leading, 14)
        .padding(.trailing, 6)
        .frame(height: 38)
        .background(.white, in: .capsule)
        .shadow(color: .black.opacity(0.08), radius: 8, y: 3)
    }
}

/// The offered layout, once kept: the pick large with its reason, then titled groups, "Other matches" last.
/// Tiles share the grid's namespace, so they move from the grid into their sections.
private struct ArrangedSections: View {
    let model: LibraryModel
    let columns: Int
    let namespace: Namespace.ID
    let open: (LibraryPage.Entry) -> Void

    var body: some View {
        let sections = model.sections
        VStack(alignment: .leading, spacing: 22) {
            HStack {
                Spacer()
                Button("Back to the grid") {
                    withAnimation(LibraryModel.morph) { model.showGrid() }
                }
                .buttonStyle(.plain)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Theme.accent)
            }
            if let pick = sections.pick {
                VStack(alignment: .leading, spacing: 8) {
                    LibraryTile(entry: pick.entry) { open(pick.entry) }
                        .matchedGeometryEffect(id: pick.entry.id, in: namespace)
                        .frame(maxWidth: 440)
                    if !pick.why.isEmpty {
                        Text(pick.why)
                            .font(.system(size: 14))
                            .foregroundStyle(Theme.ink)
                    }
                }
            }
            ForEach(Array(sections.groups.enumerated()), id: \.offset) { index, group in
                VStack(alignment: .leading, spacing: 10) {
                    Text(group.title)
                        .font(.system(size: 17, weight: .bold))
                        .foregroundStyle(Theme.ink)
                    // The last section ("Other matches") grows as more results load.
                    Masonry(entries: group.entries, columns: columns, namespace: namespace, nearEnd: {
                        if index == sections.groups.count - 1 { Task { await model.loadMore() } }
                    }, open: open)
                }
            }
        }
    }
}

/// The Mac's shelf sidebar.
private struct ShelfList: View {
    let model: LibraryModel

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("SHELVES")
                .font(.system(size: 11, weight: .semibold)).kerning(0.8)
                .foregroundStyle(Theme.quiet)
                .padding(.horizontal, 12).padding(.bottom, 6)
            row(nil, "Everything", nil)
            ForEach(Theme.areas, id: \.slug) { area in row(area.slug, area.name, Theme.color(slug: area.slug)) }
            Spacer()
        }
        .padding(.top, 96)
        .padding(.horizontal, 12)
    }

    private func row(_ slug: String?, _ name: String, _ color: Color?) -> some View {
        let selected = model.shelf == slug
        return Button {
            Task { await model.show(shelf: slug) }
        } label: {
            HStack(spacing: 10) {
                Circle().fill(color ?? Theme.ink).frame(width: 8, height: 8)
                Text(name).font(.system(size: 14, weight: selected ? .semibold : .regular))
                Spacer()
            }
            .foregroundStyle(Theme.ink)
            .padding(.horizontal, 12)
            .frame(height: 32)
            .background {
                if selected {
                    RoundedRectangle(cornerRadius: 9, style: .continuous).fill(.white)
                        .shadow(color: .black.opacity(0.06), radius: 1, y: 1)
                        .matchedGeometryEffect(id: "shelf", in: namespace)
                }
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .animation(.spring(duration: 0.3, bounce: 0.2), value: model.shelf)
    }

    @Namespace private var namespace
}

/// The iPhone's shelf chips, scrolling sideways under the title.
private struct ShelfChips: View {
    let model: LibraryModel
    @Namespace private var namespace

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                chip(nil, "Everything", nil)
                ForEach(Theme.areas, id: \.slug) { area in chip(area.slug, area.name, Theme.color(slug: area.slug)) }
            }
        }
        .scrollClipDisabled()
    }

    private func chip(_ slug: String?, _ name: String, _ color: Color?) -> some View {
        let selected = model.shelf == slug
        return Button {
            Task { await model.show(shelf: slug) }
        } label: {
            HStack(spacing: 6) {
                if let color { Circle().fill(selected ? .white : color).frame(width: 7, height: 7) }
                Text(name).font(.system(size: 14, weight: .semibold))
            }
            .foregroundStyle(selected ? .white : Theme.ink)
            .padding(.horizontal, 14)
            .frame(height: 34)
            .background {
                Capsule().fill(Theme.shelfSide)
                if selected { Capsule().fill(color ?? Theme.ink).matchedGeometryEffect(id: "chip", in: namespace) }
            }
        }
        .buttonStyle(PressScale())
        .animation(.spring(duration: 0.3, bounce: 0.2), value: model.shelf)
    }
}

/// Columns of pictures at their own heights, each added to the shortest column.
/// The Library grid. Every tile's shape is known from its picture, so the whole layout is worked out
/// once per set of saves, and only the tiles near the screen exist as views. SwiftUI's lazy stacks
/// spent three quarters of the main thread re-measuring tiles while scrolling.
private struct Masonry: View {
    let entries: [LibraryPage.Entry]
    let columns: Int
    /// Set when tiles should move between this grid and the arranged sections.
    var namespace: Namespace.ID? = nil
    /// Called when the screen gets near the bottom of what's loaded.
    let nearEnd: () -> Void
    let open: (LibraryPage.Entry) -> Void
    @State private var width: CGFloat = 0
    @State private var hovered: Int?
    /// The part of the grid on screen, in the grid's own coordinates, widened by a screen each way.
    @State private var window: ClosedRange<CGFloat> = 0...2000

    var body: some View {
        let layout = MasonryLayout(entries: entries, columns: columns, width: width)
        ZStack(alignment: .topLeading) {
            ForEach(layout.tiles.filter { $0.frame.maxY >= window.lowerBound && $0.frame.minY <= window.upperBound }) { tile in
                Group {
                    if Bench.plainTiles {
                        Theme.shelfSide  // bench control: the floor this Mac sets, with no tile work at all
                    } else {
                        LibraryTile(entry: tile.entry, slideIn: tile.frame.minY < 1200, hovering: hovered == tile.id) { open(tile.entry) }
                    }
                }
                    .frame(width: tile.frame.width, height: tile.frame.height)
                    .matched(tile.id, in: namespace)
                    // Padding, not offset: it places the tile in layout, where matchedGeometryEffect
                    // reads frames (an offset only moves the drawing).
                    .padding(.leading, tile.frame.minX)
                    .padding(.top, tile.frame.minY)
            }
        }
        .frame(maxWidth: .infinity, minHeight: layout.height, alignment: .topLeading)
        .onContinuousHover { phase in
            guard case .active(let point) = phase else { return hovered = nil }
            let over = layout.tiles.first { $0.frame.contains(point) }?.id
            if over != hovered { hovered = over }
        }
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
        .onGeometryChange(for: ClosedRange<CGFloat>.self) { proxy in
            let top = -proxy.frame(in: .scrollView).minY
            let screen = proxy.bounds(of: .scrollView)?.height ?? 1000
            // In steps of half a screen, so scrolling redraws the grid only when tiles come or go.
            let step = max(screen / 2, 200)
            let start = (top / step).rounded(.down) * step
            return (start - screen)...(start + 2 * screen)
        } action: { window in
            self.window = window
            if window.upperBound > layout.height - 400 { nearEnd() }
        }
    }
}

extension View {
    /// matchedGeometryEffect when there's a namespace to match in.
    @ViewBuilder
    func matched(_ id: Int, in namespace: Namespace.ID?) -> some View {
        if let namespace { matchedGeometryEffect(id: id, in: namespace) } else { self }
    }
}

/// Where each tile goes: the next tile into the shortest column, picture height from its shape.
struct MasonryLayout {
    struct Tile: Identifiable {
        let entry: LibraryPage.Entry
        let frame: CGRect
        var id: Int { entry.id }
    }

    static let gap: CGFloat = 14, rowGap: CGFloat = 16
    /// The caption under each picture: 6 points of space and one 15-point line.
    static let caption: CGFloat = 21

    let tiles: [Tile]
    let height: CGFloat

    init(entries: [LibraryPage.Entry], columns: Int, width: CGFloat) {
        let columnWidth = max(0, (width - Self.gap * CGFloat(columns - 1)) / CGFloat(columns))
        var heights = Array(repeating: CGFloat(0), count: columns)
        var tiles: [Tile] = []
        tiles.reserveCapacity(entries.count)
        for entry in entries {
            let column = heights.indices.min { heights[$0] < heights[$1] }!
            let pictureHeight = columnWidth / (entry.picture == nil ? 1.4 : entry.aspect)
            let frame = CGRect(x: CGFloat(column) * (columnWidth + Self.gap), y: heights[column],
                               width: columnWidth, height: pictureHeight + Self.caption)
            tiles.append(Tile(entry: entry, frame: frame))
            heights[column] = frame.maxY + Self.rowGap
        }
        self.tiles = tiles
        height = max(0, (heights.max() ?? 0) - Self.rowGap)
    }
}

struct LibraryTile: View {
    let entry: LibraryPage.Entry
    /// Slide in when the Library first shows; tiles that arrive by scrolling just appear, so a fast
    /// scroll isn't running dozens of spring animations at once.
    var slideIn = false
    /// Set by the grid, which follows the pointer once for every tile (a hover region per tile cost
    /// time on every scrolled frame).
    var hovering = false
    let open: () -> Void
    @State private var shown = false

    var body: some View {
        Button(action: open) {
            VStack(alignment: .leading, spacing: 6) {
                Color.clear
                    .aspectRatio(entry.picture == nil ? 1.4 : entry.aspect, contentMode: .fit)
                    .overlay {
                        if entry.picture == nil {
                            // A link you added whose page has no preview picture, or a GitHub star a search found.
                            VStack(alignment: .leading, spacing: 6) {
                                Image(systemName: entry.source == "github" ? "star" : "link").foregroundStyle(Theme.quiet)
                                Text(entry.repo?.name ?? entry.title ?? entry.caption)
                                    .font(.system(size: 15, weight: .semibold))
                                    .foregroundStyle(Theme.ink)
                                    .lineLimit(entry.source == "github" ? 2 : 4)
                                if entry.source == "github", let text = entry.text, !text.isEmpty {
                                    Text(text)
                                        .font(.system(size: 13))
                                        .foregroundStyle(Theme.quiet)
                                        .lineLimit(3)
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(14)
                            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                            .background(Theme.shelfSide)
                        } else {
                            TileImage(url: entry.picture, fade: slideIn)
                        }
                    }
                    .clipShape(.rect(cornerRadius: 14, style: .continuous))
                    .overlay(alignment: .topLeading) {
                        if let color = Theme.color(slug: entry.area) {
                            Circle().fill(color).frame(width: 9, height: 9)
                                .overlay(Circle().stroke(.white, lineWidth: 2))
                                .padding(10)
                        }
                    }
                    .scaleEffect(hovering ? 1.015 : 1)
                    // Only while hovered: an invisible shadow was still drawn for every tile, every frame.
                    .background {
                        if hovering {
                            RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Theme.shelf)
                                .shadow(color: .black.opacity(0.12), radius: 12, y: 6)
                        }
                    }
                Text(entry.caption)
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.quiet)
                    .lineLimit(1)
                    .frame(height: MasonryLayout.caption - 6, alignment: .leading)
            }
        }
        .buttonStyle(PressScale())
        .opacity(shown || !slideIn ? 1 : 0)
        .offset(y: shown || !slideIn ? 0 : 12)
        .onAppear { if slideIn { withAnimation(.spring(duration: 0.5)) { shown = true } } }
        .animation(.spring(duration: 0.25), value: hovering)
    }
}

extension LibraryPage.Entry {
    /// Width over height of the first picture; square when the feed doesn't know.
    var aspect: Double {
        guard let first = media.first, let width = first.width, let height = first.height, height > 0 else { return 1 }
        return min(max(width / height, 0.5), 2)
    }
}

/// One save, opened in the app: every picture, the full text, and a way out to the original.
struct SaveDetail: View {
    let entry: LibraryPage.Entry
    /// Shows the Library filtered to pictures that look like this one.
    let similar: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                #if os(macOS)
                HStack {
                    Spacer()
                    Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
                }
                #endif
                pictures

                HStack(spacing: 8) {
                    if let color = Theme.color(slug: entry.area), let area = Theme.areas.first(where: { $0.slug == entry.area }) {
                        Circle().fill(color).frame(width: 8, height: 8)
                        Text(area.name).foregroundStyle(Theme.ink)
                        Text("·")
                    }
                    Text([entry.authorName, entry.author.map { $0.hasPrefix("@") ? $0 : "@\($0)" }].compactMap { $0 }.joined(separator: " "))
                }
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(Theme.quiet)

                if let text = entry.text, !text.isEmpty {
                    Text(text).font(.system(size: 16)).foregroundStyle(Theme.ink).textSelection(.enabled)
                }
                Button {
                    dismiss()
                    similar()
                } label: {
                    Label("More like this", systemImage: "square.grid.3x3.square")
                        .font(.system(size: 16, weight: .semibold))
                        .frame(maxWidth: .infinity).frame(height: 50)
                        .foregroundStyle(Theme.ink)
                        .background(Theme.shelfSide, in: .rect(cornerRadius: 16, style: .continuous))
                }
                .buttonStyle(PressScale())
                if let link = entry.link {
                    Button { Platform.open(link) } label: {
                        Label("Open the original", systemImage: "arrow.up.right")
                            .font(.system(size: 16, weight: .semibold))
                            .frame(maxWidth: .infinity).frame(height: 50)
                            .foregroundStyle(.white)
                            .background(Theme.ink, in: .rect(cornerRadius: 16, style: .continuous))
                    }
                    .buttonStyle(PressScale())
                }
            }
            .padding(20)
        }
        .background(Theme.shelf)
        #if os(macOS)
        .frame(minWidth: 520, idealWidth: 620, maxWidth: 760, minHeight: 360, idealHeight: 600)
        #else
        .presentationDragIndicator(.visible)
        .presentationCornerRadius(34)
        #endif
    }

    private func picture(_ picture: LibraryPage.Entry.Picture) -> some View {
        AsyncImage(url: URL(string: picture.url, relativeTo: LibraryPage.mediaBase)?.absoluteURL) { image in
            image.resizable().scaledToFit()
        } placeholder: {
            Theme.shelfSide
        }
    }

    /// Swipe between pictures on the iPhone; on the Mac they stack, one under the other.
    @ViewBuilder
    private var pictures: some View {
        #if os(iOS)
        TabView {
            ForEach(entry.media, id: \.url) { picture($0) }
        }
        .tabViewStyle(.page(indexDisplayMode: entry.media.count > 1 ? .always : .never))
        .aspectRatio(entry.aspect, contentMode: .fit)
        .clipShape(.rect(cornerRadius: 18, style: .continuous))
        #else
        VStack(spacing: 10) {
            // Short enough that the text and the link sit in view under the first picture.
            ForEach(entry.media, id: \.url) { picture($0).frame(maxHeight: 380).clipShape(.rect(cornerRadius: 14, style: .continuous)) }
        }
        #endif
    }
}

/// Keep something that didn't come from Beacon: paste a link, or choose a picture.
struct AddSheet: View {
    let model: LibraryModel
    @Environment(\.dismiss) private var dismiss
    @State private var link = ""
    @State private var note = ""
    @State private var busy = false
    @State private var error: String?
    #if os(iOS)
    @State private var photo: PhotosPickerItem?
    #else
    @State private var choosing = false
    #endif

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Text("Add to your library").font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.ink)
                Spacer()
                Button { dismiss() } label: {
                    Image(systemName: "xmark").font(.system(size: 12, weight: .bold)).foregroundStyle(Theme.quiet)
                        .frame(width: 30, height: 30).background(Theme.shelfSide, in: .circle)
                }
                .buttonStyle(.plain)
                .keyboardShortcut(.cancelAction)
            }
            Text("It skips Triage and goes straight onto your Library.")
                .font(.system(size: 14)).foregroundStyle(Theme.quiet)

            HStack(spacing: 10) {
                TextField("Paste a link: any web page or X post", text: $link)
                    .textFieldStyle(.plain)
                    .font(.system(size: 15))
                    #if os(iOS)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    #endif
                    .onSubmit(addLink)
                    .padding(.horizontal, 14)
                    .frame(height: 46)
                    .background(Theme.shelfSide, in: .rect(cornerRadius: 14, style: .continuous))
                Button("Add", action: addLink)
                    .buttonStyle(.plain)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 18)
                    .frame(height: 46)
                    .background(link.isEmpty ? Theme.quiet.opacity(0.4) : Theme.ink, in: .rect(cornerRadius: 14, style: .continuous))
                    .disabled(link.isEmpty || busy)
            }

            HStack { Rectangle().fill(Theme.shelfSide).frame(height: 1); Text("or").foregroundStyle(Theme.quiet); Rectangle().fill(Theme.shelfSide).frame(height: 1) }

            TextField("A few words about the picture (optional)", text: $note)
                .textFieldStyle(.plain)
                .font(.system(size: 15))
                .padding(.horizontal, 14)
                .frame(height: 46)
                .background(Theme.shelfSide, in: .rect(cornerRadius: 14, style: .continuous))
            #if os(iOS)
            PhotosPicker(selection: $photo, matching: .images) { pictureButtonLabel }
                .disabled(busy)
                .onChange(of: photo) { _, item in
                    guard let item else { return }
                    run { model in
                        guard let data = try await item.loadTransferable(type: Data.self) else { throw PictureError() }
                        try await model.keep { try await $0.keep(picture: data, width: data.pixelSize.width, height: data.pixelSize.height, note: note) }
                    }
                }
            #else
            Button { choosing = true } label: { pictureButtonLabel }
                .buttonStyle(.plain)
                .disabled(busy)
                .fileImporter(isPresented: $choosing, allowedContentTypes: [.image]) { result in
                    guard case .success(let url) = result else { return }
                    run { model in
                        let open = url.startAccessingSecurityScopedResource()
                        defer { if open { url.stopAccessingSecurityScopedResource() } }
                        let data = try Data(contentsOf: url)
                        try await model.keep { try await $0.keep(picture: data, width: data.pixelSize.width, height: data.pixelSize.height, note: note) }
                    }
                }
            Text("Or drop pictures onto the Library.").font(.system(size: 13)).foregroundStyle(Theme.quiet)
            #endif

            if busy { HStack(spacing: 8) { ProgressView().controlSize(.small); Text("Adding…").foregroundStyle(Theme.quiet) } }
            if let error { Text(error).font(.system(size: 14)).foregroundStyle(.red) }
            Spacer(minLength: 0)
        }
        .padding(24)
        .background(Theme.shelf)
        #if os(macOS)
        .frame(width: 520, height: 400)
        #else
        .presentationDetents([.medium])
        .presentationCornerRadius(34)
        #endif
    }

    private var pictureButtonLabel: some View {
        Label("Choose a picture", systemImage: "photo.on.rectangle")
            .font(.system(size: 15, weight: .semibold))
            .foregroundStyle(Theme.ink)
            .frame(maxWidth: .infinity)
            .frame(height: 46)
            .background(Theme.shelfSide, in: .rect(cornerRadius: 14, style: .continuous))
    }

    private func addLink() {
        let url = link.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !url.isEmpty else { return }
        run { model in try await model.keep { try await $0.keep(url: url) } }
    }

    /// Runs one add, closes the sheet when it lands, or says what went wrong and stays open.
    private func run(_ work: @escaping @MainActor (LibraryModel) async throws -> Void) {
        busy = true
        error = nil
        Task {
            do {
                try await work(model)
                Platform.tap()
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
            busy = false
        }
    }
}

private struct PictureError: LocalizedError {
    var errorDescription: String? { "Couldn't read that picture." }
}

extension Data {
    /// The picture's size in pixels, for the Library's layout; 0 x 0 if it can't be read.
    var pixelSize: (width: Int, height: Int) {
        guard let source = CGImageSourceCreateWithData(self as CFData, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any] else { return (0, 0) }
        return (properties[kCGImagePropertyPixelWidth] as? Int ?? 0, properties[kCGImagePropertyPixelHeight] as? Int ?? 0)
    }
}
