import SwiftUI

struct ContentView: View {
    @Bindable var store: TriageStore

    var body: some View {
        Group {
            switch store.mode {
            case .triage: TriagePanes(store: store)
            case .briefs: NavigationStack { BriefsView(store: store).toolbar { ModePicker(store: store) } }
            }
        }
        .overlay(alignment: .bottom) { ToastView(toast: store.toast) }
        .task { await store.load() }
    }
}

struct ModePicker: ToolbarContent {
    @Bindable var store: TriageStore

    var body: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            Picker("View", selection: $store.mode) {
                Text("Triage").tag(TriageStore.Mode.triage)
                Text(store.briefCount == 0 ? "Briefs" : "Briefs (\(store.briefCount))").tag(TriageStore.Mode.briefs)
            }
            .pickerStyle(.segmented)
            .fixedSize()
        }
    }
}

/// Stacks, then the current stack's rows, then the selected item in full: Mail's layout.
struct TriagePanes: View {
    /// The Mac shows the selected row in the accent color and has a keyboard; the iPhone pushes to the post instead.
    #if os(macOS)
    static let highlightsSelection = true
    #else
    static let highlightsSelection = false
    #endif

    @Bindable var store: TriageStore
    @FocusState private var listFocused: Bool
    /// The iPhone starts on the stack list; the system moves this as you navigate.
    @State private var compactColumn = NavigationSplitViewColumn.sidebar

    var body: some View {
        NavigationSplitView(preferredCompactColumn: $compactColumn) {
            List(selection: Binding(get: { store.selectedTheme }, set: { store.selectStack($0) })) {
                Section("Stacks · \(store.totalRemaining) items") {
                    ForEach(store.stacks) { stack in
                        HStack {
                            Text(stack.theme).lineLimit(1)
                            Spacer()
                            Text("\(stack.items.count)").monospacedDigit().foregroundStyle(.secondary)
                        }
                        .tag(Optional(stack.theme))
                    }
                }
            }
            .navigationSplitViewColumnWidth(min: 190, ideal: 220, max: 280)
            #if os(iOS)
            // The split view's own toolbar isn't shown over the iPhone's stack list, where Apply lands you.
            .toolbar {
                ModePicker(store: store)
                ToolbarItem(placement: .topBarTrailing) {
                    Button { store.undo() } label: { Label("Undo", systemImage: "arrow.uturn.backward") }
                        .disabled(!store.canUndo)
                }
            }
            #endif
        } content: {
            Group {
                if store.isLoading && store.items.isEmpty {
                    ProgressView()
                } else if let error = store.loadError {
                    ContentUnavailableView {
                        Label("Couldn't Load Beacon", systemImage: "exclamationmark.triangle")
                    } description: {
                        Text(error)
                    } actions: {
                        Button("Try Again") { Task { await store.load() } }
                    }
                } else if let stack = store.currentStack {
                    rows(stack)
                } else {
                    ContentUnavailableView("All Caught Up", systemImage: "checkmark.circle",
                                           description: Text("New bookmarks arrive every Monday."))
                }
            }
            .navigationSplitViewColumnWidth(min: 320, ideal: 380, max: 460)
        } detail: {
            if let item = store.selectedItem {
                DetailPane(item: item, store: store)
            }
        }
        .toolbar {
            ModePicker(store: store)
            ToolbarItemGroup {
                Button { store.undo() } label: { Label("Undo", systemImage: "arrow.uturn.backward") }
                    .disabled(!store.canUndo)
                    .help("Undo the last Apply (⌘Z)")
                #if os(macOS)
                Button { Task { await store.sync() } } label: {
                    if store.isSyncing { ProgressView().controlSize(.small) } else { Label("Sync", systemImage: "arrow.clockwise") }
                }
                .disabled(store.isSyncing)
                .help(store.isSyncing ? "Syncing… a few minutes; keep triaging" : "Pull in new X bookmarks and GitHub stars now, score and stack them, then reload")
                #else
                Button { Task { await store.load() } } label: { Label("Reload", systemImage: "arrow.clockwise") }
                    .help("Reload the queue from Beacon")
                #endif
            }
        }
    }

    /// Header, rows, and the stack-level actions pinned under the rows they act on.
    private func rows(_ stack: Stack) -> some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                Text(stack.theme).font(.headline).lineLimit(1)
                Text("\(stack.items.count) posts · Beacon suggests \(TriageStore.summary(stack.items.reduce(into: [:]) { $0[$1.suggested, default: 0] += 1 }, keeps: stack.isArea))")
                    .font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16)
            .padding(.top, 24)
            .padding(.bottom, 12)
            Divider()
            list(stack)
            Divider()
            HStack(spacing: 8) {
                Button { store.apply() } label: {
                    HStack(spacing: 8) {
                        Text("Apply \(TriageStore.summary(store.counts(for: stack), keeps: stack.isArea))")
                        if Self.highlightsSelection { Text("⌘↩").opacity(0.7) }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.return, modifiers: .command)
                .help("Save every row's choice in this stack. ↑↓ move, D R X set a post, Return goes to the next, Space opens it.")
                Button("Reject All") { store.apply(rejectingAll: true) }
                    .help("Reject every item in this stack")
            }
            .controlSize(.large)
            .lineLimit(1)
            .padding(.horizontal, 16)
            .frame(height: DetailPane.barHeight)
        }
    }

    private func list(_ stack: Stack) -> some View {
        List(selection: $store.selectedItemID) {
            ForEach(stack.items) { item in
                Row(item: item, choice: store.choice(for: item), stack: stack, selected: Self.highlightsSelection && item.id == store.selectedItem?.id)
                    .tag(Optional(item.id))
            }
        }
        .listStyle(.inset)
        .focused($listFocused)
        .onAppear { listFocused = true }
        .onKeyPress(characters: CharacterSet(charactersIn: "drxDRX")) { press in
            let map: [String: Decision] = ["d": .doIt, "r": .read, "x": .reject]
            guard let decision = map[press.characters.lowercased()] else { return .ignored }
            store.choose(decision)
            return .handled
        }
        .onKeyPress(.return) {
            store.moveSelection(by: 1)  // keep this row's choice, go to the next
            return .handled
        }
        .onKeyPress(.space) { store.open(); return .handled }
    }
}

/// One saved item as a line you can decide on: Apply's choice, the takeaway, and why.
struct Row: View {
    let item: Item
    let choice: Decision
    let stack: Stack
    /// The selected row is drawn on the accent color, so its tag turns white to stay legible.
    let selected: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            // Same symbols as the Do / Read / Reject buttons; level with the first line, like Mail's unread dot.
            Image(systemName: choice.symbol)
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(selected ? Color.white : tint)
                .frame(width: 20, height: 20)
                .background(selected ? Color.white.opacity(0.22) : tint.opacity(0.14), in: .circle)
                .offset(y: -2)  // centers the 20pt circle on the 16pt first line
                .contentTransition(.symbolEffect(.replace))
                .animation(.spring(duration: 0.3), value: choice)
                .help("On Apply: \(choice.title)")
            VStack(alignment: .leading, spacing: 4) {
                Text(item.headline)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(choice == .reject ? .secondary : .primary)
                    .lineLimit(2)
                Text(meta)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 8)
    }

    private var tint: Color { choice == .reject ? .secondary : choice.color }

    private var meta: String {
        var parts = [item.gist?.kind.capitalized, item.handle ?? item.displayName, item.gist?.reason].compactMap { $0 }
        if let id = item.stack?.duplicateOf, let original = stack.items.first(where: { $0.id == id }) {
            parts.insert("Repeats “\(original.headline)”", at: 0)
        }
        return parts.joined(separator: " · ")
    }
}

/// The selected item in full, Beacon's read on it, and the choice for this row.
struct DetailPane: View {
    /// Shared with the rows column's footer so the two bottom bars line up across the split.
    static let barHeight: CGFloat = 80

    let item: Item
    let store: TriageStore

    var body: some View {
        let choice = store.choice(for: item)
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(item.headline)
                            .font(.system(size: 22, weight: .bold))
                            .fixedSize(horizontal: false, vertical: true)
                        if let gist = item.gist {
                            (Text("Suggests \(gist.decision.title(for: item))").foregroundStyle(gist.decision.color).fontWeight(.semibold)
                             + Text("  ·  \(item.bestProject?.name ?? "")  ·  \(gist.reason)").foregroundStyle(.secondary))
                                .font(.subheadline)
                        }
                    }
                    PostView(item: item)
                }
                .column()
                .padding(.top, 24)
                .padding(.bottom, 32)
            }
            Divider()
            // Side by side on the Mac; stacked on the narrow iPhone screen.
            (TriagePanes.highlightsSelection ? AnyLayout(HStackLayout(spacing: 16)) : AnyLayout(VStackLayout(alignment: .leading, spacing: 8))) {
                HStack(spacing: 8) {
                    ForEach([Decision.doIt, .read, .reject], id: \.self) { option in
                        BarButton(symbol: option.symbol, title: option.title(for: item), key: TriagePanes.highlightsSelection ? option.hint : nil, tint: option.color,
                                  prominent: choice == option) { store.choose(option) }
                    }
                }
                .fixedSize()
                HStack(spacing: 16) {
                    // All three explanations are laid out (two hidden) so the bar is as tall as the longest
                    // one and the buttons above it don't jump when the choice changes.
                    ZStack(alignment: .topLeading) {
                        ForEach(Decision.allCases, id: \.self) { option in
                            Text(option.consequence(for: item)).opacity(option == choice ? 1 : 0)
                        }
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    // A bounded line count, not fixedSize: the split view measures minimum heights at
                    // near-zero width, and an unbounded wrap there pushes the window taller than the screen.
                    .lineLimit(TriagePanes.highlightsSelection ? 3 : 5)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    BarButton(symbol: "arrow.down", title: "Next", key: TriagePanes.highlightsSelection ? "↩" : nil, tint: .secondary) {
                        withAnimation(.spring(duration: 0.3)) { store.moveSelection(by: 1) }
                    }
                    .disabled(!store.hasNext)
                    .opacity(store.hasNext ? 1 : 0.4)
                }
            }
            .column()
            .padding(.vertical, 12)
            .frame(minHeight: Self.barHeight)
            .animation(.spring(duration: 0.3), value: choice)
        }
    }
}

private extension View {
    /// The detail pane's reading column: the post and the decision bar share one left and right edge.
    func column() -> some View {
        frame(maxWidth: 720, alignment: .leading)
            .padding(.horizontal, 32)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct ToastView: View {
    let toast: TriageStore.Toast?

    var body: some View {
        if let toast {
            Text(toast.message)
                .font(.callout.weight(.medium))
                .foregroundStyle(toast.isError ? .white : .primary)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .background(toast.isError ? AnyShapeStyle(.red) : AnyShapeStyle(.regularMaterial), in: .capsule)
                .shadow(color: .black.opacity(0.12), radius: 12, y: 4)
                .padding(.bottom, 20)
                .transition(.move(edge: .bottom).combined(with: .opacity))
                .id(toast.id)
                .animation(.spring(duration: 0.35), value: toast)
        }
    }
}

struct BarButton: View {
    let symbol: String
    let title: String
    /// The keyboard shortcut badge; nil on the iPhone.
    let key: String?
    let tint: Color
    var prominent = false
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: symbol)
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(prominent ? .white : tint)
                Text(title)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(prominent ? .white : .primary)
                    .lineLimit(1)
                    .fixedSize()
                if let key {
                    Text(key)
                        .font(.system(size: 11, weight: .medium, design: .rounded))
                        .foregroundStyle(prominent ? .white.opacity(0.8) : .secondary)
                        .frame(minWidth: 18, minHeight: 18)
                        .background((prominent ? Color.white.opacity(0.2) : Color.primary.opacity(0.07)), in: .rect(cornerRadius: 5))
                }
            }
            .padding(.horizontal, 14)
            .frame(height: 34)
            .background {
                Capsule().fill(prominent ? AnyShapeStyle(tint) : AnyShapeStyle(tint.opacity(hovering ? 0.12 : 0.06)))
            }
            .contentShape(.capsule)
        }
        .buttonStyle(.plain)
        .onHover { over in withAnimation(.spring(duration: 0.2)) { hovering = over } }
        .help(key.map { "\(title) (\($0))" } ?? title)
    }
}
