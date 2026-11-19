import SwiftUI

@main
struct BeaconTriageApp: App {
    @NSApplicationDelegateAdaptor private var delegate: AppDelegate
    @State private var store = TriageStore(backend: BeaconCLI())

    var body: some Scene {
        Window("Beacon", id: "triage") {
            ContentView(store: store)
                .frame(minWidth: 1100, minHeight: 640)
        }
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .undoRedo) {
                Button("Undo") { store.undo() }
                    .keyboardShortcut("z")
                    .disabled(!store.canUndo)
            }
            CommandMenu("Item") {
                ForEach(Decision.allCases, id: \.self) { decision in
                    Button("\(decision.title)  \(decision.hint)") { store.choose(decision) }
                        .disabled(store.selectedItem == nil)
                }
                Divider()
                Button("Apply Stack") { store.apply() }
                    .disabled(store.currentStack == nil)
                Button("Open Link") { store.open() }
                    .keyboardShortcut("o")
                    .disabled(store.selectedItem?.link == nil)
                Button("Reload Queue") { Task { await store.load() } }
                    .keyboardShortcut("r")
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Launched from a bare binary during development: behave like a normal app.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
