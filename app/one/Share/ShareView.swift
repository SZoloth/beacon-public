import SwiftUI

/// Send to Beacon: pick the projects this link is for, say why, save. It lands in each project's queue on
/// the Mac at once, and Beacon copies it to mymind with the projects as tags and the why as a note.
struct ShareView: View {
    let url: URL?
    let sharedText: String
    let done: () -> Void

    @State private var projects: [BeaconShare.Project] = ShareView.cached
    @State private var picked: Set<String> = []
    @State private var why = ""
    @State private var state: Phase = .loading
    @FocusState private var writing: Bool

    enum Phase: Equatable { case loading, ready, offline, sending, sent, held(String) }

    private static let cacheKey = "shareProjects"
    private static var builtIn: String? { Bundle.main.object(forInfoDictionaryKey: "BeaconDefaultServer") as? String }
    private static var cached: [BeaconShare.Project] {
        guard let data = BeaconShare.shared?.data(forKey: cacheKey) else { return [] }
        return (try? JSONDecoder().decode([BeaconShare.Project].self, from: data)) ?? []
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    if let url {
                        Text(url.host().map { "\($0)\(url.path())" } ?? url.absoluteString)
                            .font(.footnote.monospaced())
                            .lineLimit(2)
                            .foregroundStyle(.secondary)
                    } else {
                        Text("There's no web link in what you shared.").foregroundStyle(.secondary)
                    }
                }
                Section {
                    TextField("Why? (optional)", text: $why, axis: .vertical)
                        .lineLimit(1...4)
                        .focused($writing)
                }
                Section {
                    if projects.isEmpty && state == .loading {
                        ProgressView()
                    }
                    ForEach(projects) { project in
                        Button { toggle(project.slug) } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(project.name).foregroundStyle(.primary)
                                    if project.id == projects.first?.id && project.matches > 0 {
                                        Text("Best guess").font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                                Spacer()
                                Image(systemName: picked.contains(project.slug) ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(picked.contains(project.slug) ? Color.accentColor : .secondary)
                                    .imageScale(.large)
                            }
                        }
                    }
                } header: {
                    Text("Projects")
                } footer: {
                    footer
                }
            }
            .navigationTitle("Send to Beacon")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel", action: done) }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saveLabel) { Task { await save() } }
                        .disabled(url == nil || state == .sending || state == .sent)
                        .bold()
                }
            }
        }
        .task { await load() }
    }

    private var saveLabel: String {
        switch state {
        case .sending: return "Saving…"
        case .sent: return "Saved"
        default: return picked.isEmpty ? "Save" : "Save to \(picked.count)"
        }
    }

    @ViewBuilder private var footer: some View {
        switch state {
        case .offline:
            Text("Can't reach your Mac. Saving keeps it on this phone, and Beacon sends it the next time you open the app.")
        case .held(let reason):
            Text("\(reason) Saved on this phone; Beacon sends it the next time you open the app.")
        case .ready where picked.isEmpty:
            Text("With no project picked, it goes to your library and mymind only.")
        default:
            EmptyView()
        }
    }

    private func toggle(_ slug: String) {
        if picked.contains(slug) { picked.remove(slug) } else { picked.insert(slug) }
    }

    private func load() async {
        guard let base = BeaconShare.base(builtIn: Self.builtIn) else { state = .offline; return }
        do {
            let fresh = try await BeaconShare.projects(text: [sharedText, url?.absoluteString ?? ""].joined(separator: " "), base: base)
            projects = fresh
            BeaconShare.shared?.set(try? JSONEncoder().encode(fresh), forKey: Self.cacheKey)
            if let first = fresh.first, first.matches > 0, picked.isEmpty { picked = [first.slug] }
            state = .ready
        } catch {
            state = .offline
        }
    }

    private func save() async {
        guard let url else { return }
        writing = false
        let order = projects.map(\.slug)
        let save = BeaconShare.Save(url: url.absoluteString,
                                  projects: picked.sorted { (order.firstIndex(of: $0) ?? 99) < (order.firstIndex(of: $1) ?? 99) },
                                  note: why.trimmingCharacters(in: .whitespacesAndNewlines))
        state = .sending
        do {
            guard let base = BeaconShare.base(builtIn: Self.builtIn) else { throw BeaconShare.Failure(errorDescription: "Can't reach Beacon on the Mac.") }
            try await BeaconShare.send(save, base: base)
            state = .sent
            try? await Task.sleep(for: .milliseconds(450))
        } catch {
            BeaconShare.hold(save)
            state = .held(error.localizedDescription)
            try? await Task.sleep(for: .seconds(1.6))
        }
        done()
    }
}
