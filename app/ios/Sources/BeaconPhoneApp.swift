import SwiftUI

@main
struct BeaconPhoneApp: App {
    @State private var store = TriageStore(backend: BeaconHTTP())

    var body: some Scene {
        WindowGroup {
            ContentView(store: store)
        }
    }
}
