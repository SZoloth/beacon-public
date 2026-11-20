import SwiftUI
#if os(macOS)
import AppKit
#endif

@main
struct BeaconApp: App {
    #if os(macOS)
    static let backend: any Backend = BeaconCLI()
    #else
    static let backend: any Backend = BeaconHTTP()
    #endif

    @State private var store = TriageStore(backend: BeaconApp.backend)
    @State private var activity = ActivityModel(backend: BeaconApp.backend)
    @State private var pick = PickModel(backend: BeaconApp.backend)
    #if os(macOS)
    // The Library asks the running server, not a new `beacon` process per page (about a second each),
    // unless another checkout is configured: then it reads through that checkout's CLI, like Triage.
    @State private var library = LibraryModel(
        backend: BeaconCLI.usesSharedServer
            ? BeaconHTTP(server: URL(string: "http://127.0.0.1:8765")!)
            : BeaconApp.backend)
    #else
    @State private var library = LibraryModel(backend: BeaconApp.backend)
    #endif

    init() {
        Bench.start()
        // Library and Triage pictures come back from the cache instead of the network.
        URLCache.shared = URLCache(memoryCapacity: 96 << 20, diskCapacity: 1 << 30)
        #if os(iOS)
        LibraryPage.mediaBase = BeaconHTTP().base
        #endif
    }

    var body: some Scene {
        #if os(macOS)
        WindowGroup("Beacon", id: "beacon") {
            RootView(store: store, activity: activity, pick: pick, library: library)
                .handlesExternalEvents(preferring: ["*"], allowing: ["*"])
                // Ideal and max sizes given, so the window never asks the pages how big they'd like
                // to be: answering meant measuring the whole Library grid and Triage list, on every
                // frame of any animation (0.9 s of stalled frames per tab switch).
                .frame(minWidth: 1100, idealWidth: 1440, maxWidth: .infinity,
                       minHeight: 680, idealHeight: 900, maxHeight: .infinity)
        }
        .handlesExternalEvents(matching: ["*"])
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {} // Keep one main window; URL events reuse it.
            CommandGroup(replacing: .undoRedo) {
                Button("Undo") { store.undo() }
                    .keyboardShortcut("z")
                    .disabled(!store.canUndo)
            }
            CommandGroup(after: .toolbar) {
                Button("Reload") { Task { await store.load() } }.keyboardShortcut("r")
            }
            CommandGroup(after: .pasteboard) {
                // Universal Clipboard carries it to the phone, where Today offers to paste it.
                Button("Copy Key for iPhone") { PhoneKey.copyToClipboard() }
                PairMenuButton()
            }
        }
        Window("Pair iPhone", id: "pair") { PairView() }
            .windowResizability(.contentSize)
        #else
        WindowGroup {
            RootView(store: store, activity: activity, pick: pick, library: library)
        }
        #endif
    }
}

enum AppTab: String, CaseIterable, Identifiable {
    case triage = "Triage", activity = "Activity", pick = "Pick", library = "Library"
    var id: Self { self }

    var symbol: String {
        switch self {
        case .triage: "tray"
        case .library: "square.grid.2x2"
        case .activity: "sparkles.rectangle.stack"
        case .pick: "rectangle.split.2x1"
        }
    }
}

struct RootView: View {
    @Bindable var store: TriageStore
    let activity: ActivityModel
    let pick: PickModel
    let library: LibraryModel
    /// Reopens on the tab you left.
    @AppStorage("tab") private var tab = AppTab.triage
    /// Tabs opened so far. Each is built the first time, then kept and only hidden: rebuilding on
    /// every switch cost up to 1.3 s of stalled frames (Triage) and lost the Library's scroll position.
    @State private var built: Set<AppTab> = []
    #if os(iOS)
    @State private var pendingPair: Pairing?
    #endif

    var body: some View {
        content
            // The pages are drawn light (yellow and white); system text must not turn white in Dark Mode.
            .preferredColorScheme(.light)
            .tint(Theme.ink)
            .overlay(alignment: .bottom) { ToastView(toast: activity.toast ?? pick.toast ?? store.toast).padding(.bottom, Self.toastLift) }
            .onChange(of: store.items.isEmpty, initial: true) {
                if !store.items.isEmpty { Bench.mark("launch_to_triage_shown_ms") }
            }
            .task {
                store.onQueueLoaded = { items in Task { await WidgetPublisher.picks(items) } }
                await store.load()
                Bench.mark("launch_to_triage_loaded_ms")
                await activity.load()
            }
            .background { if Bench.enabled { BenchSwitchDriver() } }
            .onOpenURL { url in
                guard let link = BeaconLink(url) else { return }
                switch link {
                case .triage: tab = .triage
                case .save(let id):
                    tab = .library
                    Task { await library.openSave(id) }
                case .pair(let server, let key):
                    #if os(iOS)
                    pendingPair = Pairing(server: server, key: key)
                    #endif
                    _ = (server, key)
                }
            }
            #if os(iOS)
            .alert("Pair with \(pendingPair?.server.host?.split(separator: ".").first.map(String.init) ?? "this Mac")?",
                   isPresented: Binding(get: { pendingPair != nil }, set: { if !$0 { pendingPair = nil } })) {
                Button("Pair") {
                    if let pair = pendingPair {
                        UserDefaults.standard.set(pair.server.absoluteString, forKey: BeaconHTTP.serverKey)
                        UserDefaults.standard.set(pair.key, forKey: BeaconHTTP.accessKeyKey)
                        LibraryPage.mediaBase = pair.server
                        Task { await store.load(); await activity.load() }
                    }
                    pendingPair = nil
                }
                Button("Cancel", role: .cancel) { pendingPair = nil }
            } message: {
                Text("Beacon on this phone will use \(pendingPair?.server.host ?? "that Mac") and its access key.")
            }
            #endif
    }

    @ViewBuilder
    private var content: some View {
        #if os(macOS)
        ZStack(alignment: .top) {
            ForEach(AppTab.allCases) { option in
                if built.contains(option) || option == tab {
                    page(option)
                        .opacity(option == tab ? 1 : 0)
                        .disabled(option != tab)  // a hidden tab takes no clicks, keys or shortcuts
                        .accessibilityHidden(option != tab)
                        .zIndex(option == tab ? 1 : 0)
                        // Pages switch instantly; only the picker's capsule springs. A cross-fade of whole
                        // pages stalled frames for ~400 ms. (value: tab, so animations inside pages stay.)
                        .transaction(value: tab) { $0.animation = nil }
                }
            }
        }
        // An overlay, not a sibling in the stack: the capsule's spring then can't re-lay out the pages
        // on every frame of the animation.
        .overlay(alignment: .topTrailing) {
            TabPicker(tab: $tab, briefs: waiting,
                      planning: !store.planningRows.isEmpty || activity.records.contains { $0.state == .running })
                .padding(.top, 14)
                .padding(.trailing, 24)
        }
        .onChange(of: tab, initial: true) { built.insert(tab) }
        // No cross-fade: fading three whole pages cost about 400 ms of stalled frames per switch.
        .ignoresSafeArea(edges: .top)
        #else
        TabView(selection: $tab) {
            ForEach(AppTab.allCases) { tab in
                Group {
                    switch tab {
                    case .triage: TodayView(store: store)
                    case .library: LibraryView(model: library)
                    case .activity: ActivityPage(store: store, activity: activity)
                    case .pick: PickPage(model: pick)
                    }
                }
                .tabItem { Label(tab.rawValue, systemImage: tab.symbol) }
                .badge(tab == .activity ? waiting : 0)
                .tag(tab)
            }
        }
        #endif
    }
}

extension RootView {
    /// The Activity badge: plans to start plus hand-offs waiting on you.
    private var waiting: Int { activity.toActOn(store.plans) }

    #if os(macOS)
    @ViewBuilder
    private func page(_ option: AppTab) -> some View {
        switch option {
        case .triage: TodayView(store: store, active: tab == .triage)
        case .library: LibraryView(model: library)
        case .activity: ActivityPage(store: store, activity: activity, active: tab == .activity)
        case .pick: PickPage(model: pick, active: tab == .pick)
        }
    }
    #endif

    /// Above the iPhone's floating tab bar.
    #if os(macOS)
    static let toastLift: CGFloat = 0
    #else
    static let toastLift: CGFloat = 76
    #endif
}

/// The Mac's mode switch: a small capsule like Family's, top right.
struct TabPicker: View {
    @Binding var tab: AppTab
    let briefs: Int
    /// An agent is planning Do picks or running a hand-off: Activity shows a turning sparkle until it lands.
    let planning: Bool
    @Namespace private var namespace

    /// Two places, two controls: the work (Triage, then the Activity it produces) and the Library you keep.
    var body: some View {
        HStack(spacing: 16) {
            group([.triage, .activity, .pick], fill: Theme.control)
            group([.library], fill: Theme.shelfSide)
                .overlay(Capsule().strokeBorder(.black.opacity(0.06)))
        }
        .animation(.spring(duration: 0.35), value: briefs)
        .animation(.spring(duration: 0.35), value: planning)
    }

    private func group(_ options: [AppTab], fill: Color) -> some View {
        HStack(spacing: 2) {
            ForEach(options) { option in button(option) }
        }
        .padding(4)
        .background(fill, in: .capsule)
    }

    private func button(_ option: AppTab) -> some View {
        Button {
            withAnimation(.spring(duration: 0.32, bounce: 0.2)) { tab = option }
        } label: {
            HStack(spacing: 5) {
                if option == .library { Image(systemName: option.symbol).font(.system(size: 11, weight: .semibold)) }
                Text(option.rawValue)
                if option == .activity && planning {
                    Image(systemName: "sparkle")
                        .foregroundStyle(Theme.accent)
                        .symbolEffect(.rotate, options: .repeating)
                        .transition(.scale.combined(with: .opacity))
                }
                if option == .activity && briefs > 0 {
                    Text("\(briefs)").foregroundStyle(Theme.quiet).monospacedDigit()
                        .contentTransition(.numericText())
                }
            }
            .font(.system(size: 13, weight: tab == option ? .semibold : .medium))
            .foregroundStyle(tab == option ? Theme.ink : Theme.quiet)
            .padding(.horizontal, 14)
            .frame(height: 30)
            .background {
                if tab == option {
                    Capsule().fill(.white).shadow(color: .black.opacity(0.08), radius: 1, y: 1)
                        .matchedGeometryEffect(id: "tab", in: namespace)
                }
            }
            .contentShape(.capsule)
        }
        .buttonStyle(.plain)
    }
}

#if os(macOS)
/// The phone's access key, which `beacon serve` keeps in ~/.config/beacon/serve-key.
enum PhoneKey {
    static func read() -> String? {
        let path = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/beacon/serve-key")
        guard let key = (try? String(contentsOf: path, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines),
              !key.isEmpty else { return nil }
        return key
    }

    static func copyToClipboard() {
        guard let key = read() else { NSSound.beep(); return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(key, forType: .string)
    }
}
#endif

#if os(iOS)
struct Pairing: Equatable { let server: URL; let key: String }
#endif
