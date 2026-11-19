import SwiftUI

/// Reference review: say what you're making, and Beacon says which of the saves shown fit it,
/// best first, and which repeat a picture you already have. Pictures are compared on the Mac;
/// with the switch on, Jev also reads their words.
struct ReferenceSheet: View {
    let model: LibraryModel
    @Environment(\.dismiss) private var dismiss
    @AppStorage("referenceBrief") private var brief = ""
    @AppStorage("referenceWords") private var words = true
    @State private var result: ReferenceReview?
    @State private var busy = false
    @State private var error: String?
    @State private var opened: LibraryPage.Entry?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            if let result {
                results(result)
            } else {
                form
            }
        }
        .padding(24)
        .background(Theme.shelf)
        .animation(.spring(duration: 0.4), value: result)
        .sheet(item: $opened) { entry in
            SaveDetail(entry: entry) {
                dismiss()
                Task { await model.showSimilar(to: entry) }
            }
        }
        #if os(macOS)
        .frame(width: 640, height: result == nil ? 420 : 680)
        #else
        .presentationDetents([.large])
        .presentationCornerRadius(34)
        #endif
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Review references").font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.ink)
                Text("Checks the \(model.reviewable.count == 40 ? "first 40" : "\(model.reviewable.count)") saves shown")
                    .font(.system(size: 14)).foregroundStyle(Theme.quiet).lineLimit(1)
            }
            Spacer()
            Button { dismiss() } label: {
                Image(systemName: "xmark").font(.system(size: 12, weight: .bold)).foregroundStyle(Theme.quiet)
                    .frame(width: 30, height: 30).background(Theme.shelfSide, in: .circle)
            }
            .buttonStyle(.plain)
            .keyboardShortcut(.cancelAction)
        }
    }

    private var form: some View {
        VStack(alignment: .leading, spacing: 16) {
            TextField("What are you making? For example: a warm kitchen with wood and plants", text: $brief, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 16))
                .lineLimit(2...4)
                .padding(14)
                .background(Theme.shelfSide, in: .rect(cornerRadius: 14, style: .continuous))
                .onChange(of: brief) { _, text in
                    // Return runs the review instead of starting a new line.
                    guard text.contains("\n") else { return }
                    brief = text.replacingOccurrences(of: "\n", with: "")
                    review()
                }
            Toggle(isOn: $words) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Also have Jev read their words").font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.ink)
                    Text("Sends your sentence and the posts' text to TypeSafe. Pictures are always compared on the Mac.")
                        .font(.system(size: 13)).foregroundStyle(Theme.quiet)
                }
            }
            .tint(Theme.accent)
            Button(action: review) {
                Text(busy ? "Reviewing… about 5 seconds" : "Review \(model.reviewable.count) saves")
                    .font(.system(size: 16, weight: .semibold))
                    .frame(maxWidth: .infinity).frame(height: 50)
                    .foregroundStyle(.white)
                    .background(canReview ? Theme.ink : Theme.quiet.opacity(0.4), in: .rect(cornerRadius: 16, style: .continuous))
            }
            .buttonStyle(PressScale())
            .disabled(!canReview)
            if let error { Text(error).font(.system(size: 14)).foregroundStyle(.red) }
            Spacer(minLength: 0)
        }
    }

    private var canReview: Bool {
        !busy && !model.reviewable.isEmpty && !brief.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func results(_ result: ReferenceReview) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("For “\(brief.trimmingCharacters(in: .whitespacesAndNewlines))”")
                        .font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.ink).lineLimit(2)
                    Text(summary(result)).font(.system(size: 13)).foregroundStyle(Theme.quiet)
                }
                Spacer()
                Button("Change") { self.result = nil }
                    .buttonStyle(.plain)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Theme.accent)
            }
            if let note = result.note {
                Text(note).font(.system(size: 13)).foregroundStyle(Theme.quiet)
            }
            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(result.saves) { save in
                        if let entry = model.reviewable.first(where: { $0.id == save.id }) {
                            Button { opened = entry } label: { row(save, entry) }
                                .buttonStyle(PressScale())
                        }
                    }
                }
            }
        }
    }

    private func summary(_ result: ReferenceReview) -> String {
        let count = { (fit: String) in result.saves.filter { $0.fit == fit && $0.duplicateOf == nil }.count }
        let repeats = result.saves.filter { $0.duplicateOf != nil }.count
        var parts = ["\(count("strong")) strong", "\(count("maybe")) maybe", "\(count("off")) off brief"]
        if repeats > 0 { parts.append("\(repeats) \(repeats == 1 ? "repeat" : "repeats")") }
        return parts.joined(separator: " · ") + (result.wordsChecked ? "" : " · pictures only")
    }

    private func row(_ save: ReferenceReview.Save, _ entry: LibraryPage.Entry) -> some View {
        HStack(spacing: 12) {
            Group {
                if let picture = entry.picture {
                    TileImage(url: picture, pixels: 192)
                } else {
                    Image(systemName: "link").foregroundStyle(Theme.quiet).frame(maxWidth: .infinity, maxHeight: .infinity).background(Theme.shelfSide)
                }
            }
            .frame(width: 64, height: 64)
            .clipShape(.rect(cornerRadius: 10, style: .continuous))
            VStack(alignment: .leading, spacing: 3) {
                Text(entry.caption.isEmpty ? "Picture" : entry.caption)
                    .font(.system(size: 14, weight: .medium)).foregroundStyle(Theme.ink).lineLimit(2)
                Text(save.reason).font(.system(size: 12)).foregroundStyle(Theme.quiet).lineLimit(2)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            FitPill(save: save)
        }
        .padding(8)
        .background(Theme.shelfSide.opacity(save.duplicateOf == nil ? 1 : 0.5), in: .rect(cornerRadius: 14, style: .continuous))
        .opacity(save.duplicateOf == nil && save.fit != "off" ? 1 : 0.6)
        .contentShape(.rect)
    }

    private func review() {
        guard canReview else { return }
        busy = true
        error = nil
        Task {
            do {
                result = try await model.review(brief: brief.trimmingCharacters(in: .whitespacesAndNewlines), words: words)
                Platform.tap()
            } catch {
                self.error = error.localizedDescription
            }
            busy = false
        }
    }
}

private struct FitPill: View {
    let save: ReferenceReview.Save

    var body: some View {
        let (label, text, fill): (String, Color, Color) =
            save.duplicateOf != nil ? ("Repeat", Theme.quiet, Theme.shelf)
            : save.fit == "strong" ? ("Strong", .white, Theme.accent)
            : save.fit == "maybe" ? ("Maybe", Theme.ink, Theme.control)
            : ("Off", Theme.quiet, Theme.shelf)
        Text(label)
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(text)
            .padding(.horizontal, 10)
            .frame(height: 24)
            .background(fill, in: .capsule)
    }
}
