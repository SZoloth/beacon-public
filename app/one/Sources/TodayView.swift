import SwiftUI

/// The yellow half of the app, where you decide. Autopilot passes the noise and sends reading to
/// Reader on its own, so Triage shows today's few picks, then what Beacon handled, with Undo. Each
/// row carries Beacon's suggestion: tap the pill to accept it, or the row for a tray with the post,
/// Beacon's reason and all three choices.
struct TodayView: View {
    @Bindable var store: TriageStore
    /// The tab on screen. Triage stays alive behind the other tabs, so it takes the keyboard back
    /// when you return to it.
    var active = true
    @FocusState private var focused: Bool
    @State private var selectedID: Int?
    /// Choices still playing their exit: the row shows a check, then leaves the way its choice goes.
    @State private var leaving: [Int: Decision] = [:]
    /// +1 when moving down the list, -1 up: the Mac tray slides in from that side.
    @State private var travel = 1
    /// Everything else still queued, under the picks; hidden until asked for.
    @State private var showingRest = false
    @State private var showingAllHandled = false

    private var rest: [Item] {
        let picked = Set(store.picks.map(\.id))
        return store.open.filter { !picked.contains($0.id) }
    }
    private var rows: [Item] { store.picks + (showingRest ? rest : []) }
    private var selected: Item? { rows.first { $0.id == selectedID } }

    var body: some View {
        #if os(macOS)
        HStack(spacing: 0) {
            list.frame(minWidth: 460, idealWidth: 580, maxWidth: 620)
            ZStack(alignment: .top) {
                Theme.deep.ignoresSafeArea()
                if let item = selected ?? rows.first {
                    TrayView(item: item, decide: { decide(item, $0) }, close: nil)
                        .frame(maxWidth: 560)
                        .padding(.top, 70)
                        .padding(.horizontal, 32)
                        .padding(.bottom, 24)
                        .id(item.id)
                        .transition(.asymmetric(
                            insertion: .offset(y: CGFloat(travel) * 60).combined(with: .opacity),
                            removal: .offset(y: CGFloat(-travel) * 40).combined(with: .opacity)))
                } else if !store.isLoading {
                    CaughtUp().padding(.top, 200)
                }
            }
            .frame(maxWidth: .infinity)
        }
        .animation(.spring(duration: 0.38, bounce: 0.18), value: selected?.id)
        .focusable()
        .focused($focused)
        .focusEffectDisabled()
        .onChange(of: active, initial: true) { focused = active }
        .onKeyPress(keys: [.upArrow, .downArrow]) { press in
            move(by: press.key == .upArrow ? -1 : 1)
            return .handled
        }
        .onKeyPress(characters: .letters) { press in
            guard let item = selected ?? rows.first else { return .ignored }
            switch press.characters.lowercased() {
            case "d", "k": decide(item, .doIt)
            case "r": decide(item, .read)
            case "x", "p": decide(item, .reject)
            default: return .ignored
            }
            return .handled
        }
        #else
        ZStack(alignment: .bottom) {
            list
            if let item = selected {
                Color.black.opacity(0.28).ignoresSafeArea()
                    .onTapGesture { selectedID = nil }
                    .transition(.opacity)
                TrayView(item: item, decide: { decide(item, $0) }) { selectedID = nil }
                    .padding(.horizontal, 8)
                    .padding(.bottom, 4)
                    .transition(.move(edge: .bottom).combined(with: .scale(scale: 0.96, anchor: .bottom)))
                    .zIndex(1)
            }
        }
        .animation(.spring(duration: 0.45, bounce: 0.22), value: selected?.id)
        .toolbar(selected == nil ? .visible : .hidden, for: .tabBar)
        #endif
    }

    private var list: some View {
        // Grouping and sorting ~400 saves is the costly part: do it once per redraw, not per row.
        let rows = rows
        let highlighted = rows.first { $0.id == selectedID }?.id ?? defaultSelection(in: rows)?.id
        return ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                header
                if store.isLoading && store.items.isEmpty {
                    ProgressView().frame(maxWidth: .infinity).padding(.top, 80)
                } else if let error = store.loadError {
                    Message(title: "Can't reach Beacon", text: error)
                } else if store.picks.isEmpty {
                    CaughtUp().frame(maxWidth: .infinity).padding(.top, 80)
                }
                if !store.picks.isEmpty { SectionTitle(title: "Today's picks", count: store.picks.count) }
                ForEach(store.picks) { row($0, in: rows, highlighted: highlighted) }
                let rest = rest
                if !rest.isEmpty {
                    QuietButton(title: showingRest ? "Hide the other \(rest.count)" : "Show \(rest.count) more") {
                        withAnimation(.spring(duration: 0.4)) { showingRest.toggle() }
                    }
                    if showingRest { ForEach(rest) { row($0, in: rows, highlighted: highlighted) } }
                }
                if !store.handled.isEmpty {
                    SectionTitle(title: "Handled by Beacon", count: store.handled.count)
                    ForEach(showingAllHandled ? store.handled : Array(store.handled.prefix(5))) { entry in
                        HandledRow(entry: entry) { withAnimation(.spring(duration: 0.4)) { store.unhandle(entry) } }
                            .transition(.opacity)
                    }
                    if store.handled.count > 5 {
                        QuietButton(title: showingAllHandled ? "Show fewer" : "Show all \(store.handled.count)") {
                            withAnimation(.spring(duration: 0.4)) { showingAllHandled.toggle() }
                        }
                    }
                }
            }
            .padding(.horizontal, Self.gutter)
            .padding(.bottom, 40)
            .animation(.spring(duration: 0.42, bounce: 0.15), value: store.open.map(\.id))
        }
        .scrollContentBackground(.hidden)
        .background(Theme.page)
        .refreshable { await store.load() }
    }

    private var header: some View {
        HStack(alignment: .lastTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(Date.now, format: .dateTime.weekday(.wide).month(.wide).day())
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(Theme.quiet)
                Text("Triage")
                    .font(.system(size: Self.titleSize, weight: .heavy))
                    .kerning(-1.2)
                    .foregroundStyle(Theme.ink)
            }
            Spacer()
            #if os(iOS)
            if store.canUndo {
                Button("Undo") { withAnimation(.spring(duration: 0.4)) { store.undo() } }
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                    .padding(.horizontal, 14).padding(.vertical, 7)
                    .background(Theme.control, in: .capsule)
                    .transition(.scale.combined(with: .opacity))
            }
            #endif
            Text(store.items.isEmpty && store.isLoading ? " " : "\(store.picks.count) picks today")
                .font(.system(size: 15, weight: .semibold).monospacedDigit())
                .contentTransition(.numericText(countsDown: true))
                .foregroundStyle(Theme.quiet)
        }
        .animation(.spring(duration: 0.4), value: store.picks.count)
        .animation(.spring(duration: 0.3), value: store.canUndo)
        .padding(.top, Self.headerTop)
    }

    private func row(_ item: Item, in rows: [Item], highlighted: Int?) -> some View {
        TodayRow(item: item, isSelected: item.id == highlighted, leaving: leaving[item.id]) {
            travel = (rows.firstIndex(of: item) ?? 0) >= (rows.firstIndex { $0.id == selectedID } ?? 0) ? 1 : -1
            selectedID = item.id
        } accept: {
            decide(item, item.suggested)
        }
        .transition(.asymmetric(insertion: .opacity, removal: exit(leaving[item.id])))
    }

    private func defaultSelection(in rows: [Item]) -> Item? {
        #if os(macOS)
        rows.first
        #else
        nil
        #endif
    }

    /// Where a row goes: Do and Keep slide right toward Briefs and the shelf, Read lifts away, Pass drops left.
    private func exit(_ decision: Decision?) -> AnyTransition {
        switch decision {
        case .doIt: .move(edge: .trailing).combined(with: .scale(scale: 0.8, anchor: .trailing)).combined(with: .opacity)
        case .read: .offset(y: -30).combined(with: .opacity)
        case .reject: .move(edge: .leading).combined(with: .opacity)
        case nil: .opacity
        }
    }

    /// The row shows a check for a beat, then leaves in its choice's direction.
    private func decide(_ item: Item, _ decision: Decision) {
        guard leaving[item.id] == nil else { return }
        let next = rows.firstIndex(of: item).flatMap { rows.indices.contains($0 + 1) ? rows[$0 + 1] : nil }
        withAnimation(.spring(duration: 0.25, bounce: 0.4)) { leaving[item.id] = decision }
        #if os(iOS)
        selectedID = nil
        #endif
        Task {
            try? await Task.sleep(for: .milliseconds(260))
            withAnimation(.spring(duration: 0.42, bounce: 0.15)) {
                store.decide(item, decision, named: decision == .reject ? "Passed" : decision.label(for: item))
                #if os(macOS)
                if selectedID == nil || selectedID == item.id { travel = 1; selectedID = next?.id }
                #endif
            }
            leaving[item.id] = nil
        }
    }

    private func move(by offset: Int) {
        guard let current = selected ?? rows.first, let index = rows.firstIndex(of: current) else { return }
        travel = offset > 0 ? 1 : -1
        selectedID = rows[min(max(index + offset, 0), rows.count - 1)].id
    }

    #if os(macOS)
    static let gutter: CGFloat = 40
    static let titleSize: CGFloat = 52
    static let headerTop: CGFloat = 24
    #else
    static let gutter: CGFloat = 20
    static let titleSize: CGFloat = 36
    static let headerTop: CGFloat = 8
    #endif
}

/// The rare moment: nothing left to sort.
/// One thing autopilot did, with the way back.
private struct HandledRow: View {
    let entry: HandledItem
    let undo: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(entry.title?.split(separator: "\n").first.map(String.init) ?? "Save \(entry.id)")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(1)
                Text([entry.verb, entry.reason].compactMap { $0 }.joined(separator: " · "))
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.quiet)
                    .lineLimit(2)
            }
            Spacer(minLength: 8)
            Button("Undo", action: undo)
                .buttonStyle(.plain)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Theme.ink)
                .padding(.horizontal, 12).padding(.vertical, 5)
                .background(Theme.control, in: .capsule)
        }
        .padding(.vertical, 10)
    }
}

private struct QuietButton: View {
    let title: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(Theme.ink)
                .padding(.horizontal, 14).padding(.vertical, 8)
                .background(Theme.control, in: .capsule)
        }
        .buttonStyle(.plain)
        .padding(.top, 14)
    }
}

private struct CaughtUp: View {
    @State private var bounce = 0

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "sun.max.fill")
                .font(.system(size: 56))
                .foregroundStyle(Color(hex: 0xF0A43A))
                .symbolEffect(.bounce, value: bounce)
                .symbolEffect(.rotate, options: .speed(0.15).repeating)
            Text("All caught up").font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.ink)
            Text("New saves show up here after the Monday run.").foregroundStyle(Theme.quiet)
        }
        .onAppear { bounce += 1 }
    }
}

private struct Message: View {
    let title: String
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 20, weight: .bold)).foregroundStyle(Theme.ink)
            Text(text).foregroundStyle(Theme.quiet)
        }
        .padding(.top, 40)
    }
}

struct TodayRow: View {
    let item: Item
    let isSelected: Bool
    let leaving: Decision?
    let open: () -> Void
    let accept: () -> Void

    var body: some View {
        HStack(spacing: 14) {
            Thumbnail(item: item, size: CGSize(width: 52, height: 52), corner: 12)
            VStack(alignment: .leading, spacing: 4) {
                Text(item.headline)
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                ShelfLabel(project: item.bestProject)
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.quiet)
            }
            // The row opens with a tap rather than a Button because the pill inside is a Button of its
            // own; VoiceOver gets the words as one button, and the pill stays separate.
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(.isButton)
            .accessibilityHint("Shows the post and every choice")
            .accessibilityAction { open() }
            Spacer(minLength: 8)
            SuggestionPill(item: item, done: leaving, action: accept)
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 10)
        .background {
            if isSelected {
                RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Theme.tray)
                    .shadow(color: .black.opacity(0.05), radius: 6, y: 2)
            }
        }
        .contentShape(.rect)
        .onTapGesture(perform: open)
        .padding(.horizontal, -10)
        .animation(.spring(duration: 0.3), value: isSelected)
    }
}

/// Beacon's suggestion as a tappable pill: one tap accepts it, and it turns into a check.
struct SuggestionPill: View {
    let item: Item
    let done: Decision?
    let action: () -> Void

    var body: some View {
        let decision = done ?? item.suggested
        let area = Theme.color(for: item.bestProject)
        Button(action: action) {
            ZStack {
                if done != nil {
                    Image(systemName: "checkmark")
                        .font(.system(size: 12, weight: .heavy))
                        .accessibilityLabel("Done")
                        .transition(.scale(scale: 0.3).combined(with: .opacity))
                } else {
                    Text(decision.label(for: item))
                        .font(.system(size: 13, weight: .semibold))
                        .transition(.scale(scale: 0.8).combined(with: .opacity))
                }
            }
            .padding(.horizontal, done == nil ? 13 : 9)
            .frame(minWidth: 26, minHeight: 26)
            .foregroundStyle(foreground(decision, area))
            .background(background(decision, area), in: .capsule)
        }
        .buttonStyle(PressScale())
        .disabled(done != nil)
        .help("Accept: \(decision.label(for: item))")
    }

    private func foreground(_ decision: Decision, _ area: Color?) -> Color {
        if done != nil { return .white }
        return decision == .doIt ? (area ?? .white) : Theme.quiet
    }

    private func background(_ decision: Decision, _ area: Color?) -> Color {
        if done != nil { return decision == .doIt ? (area ?? Theme.ink) : decision == .read ? Theme.accent : Theme.quiet }
        return decision == .doIt ? (area.map { $0.opacity(0.16) } ?? Theme.ink) : Theme.control
    }
}

/// The save's first picture, or its source symbol on a soft square.
struct Thumbnail: View {
    let item: Item
    let size: CGSize
    let corner: CGFloat

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: corner, style: .continuous)
        ZStack {
            Theme.soft
            if let url = item.preview?.media?.first?.imageURL {
                // Through the thumbnail cache, not AsyncImage, which decoded the full-size picture.
                TileImage(url: url, pixels: Int(max(size.width, size.height) * 3), background: Theme.soft)
            } else {
                Image(systemName: item.isGitHub ? "star.fill" : "text.alignleft")
                    .font(.system(size: size.width * 0.32, weight: .semibold))
                    .foregroundStyle(Theme.quiet.opacity(0.6))
            }
        }
        .frame(width: size.width, height: size.height)
        .clipShape(shape)
        .overlay(shape.strokeBorder(.black.opacity(0.06), lineWidth: 1))
        .accessibilityHidden(true)
    }
}

extension Decision {
    /// Gentler words for the one app: Pass instead of Reject, Keep instead of Do on an area.
    func label(for item: Item) -> String {
        self == .reject ? "Pass" : title(for: item)
    }
}
