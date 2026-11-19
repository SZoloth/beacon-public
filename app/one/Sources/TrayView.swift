import SwiftUI

/// One save, one decision. The post is open from the start; Beacon's reason and what each
/// choice will do sit under it, with the three choices at the bottom.
struct TrayView: View {
    let item: Item
    let decide: (Decision) -> Void
    /// nil on the Mac, where the tray is a pane and moving on replaces it.
    var close: (() -> Void)?
    @State private var hovered: Decision?
    @State private var shown = false

    private var focus: Decision { hovered ?? item.suggested }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                ShelfLabel(project: item.bestProject)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                Spacer()
                if let close {
                    Button(action: close) {
                        Image(systemName: "xmark")
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(Theme.quiet)
                            .frame(width: 30, height: 30)
                            .background(Theme.soft, in: .circle)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Close")
                }
            }

            // A short post takes only its own height; a long one scrolls inside the tray.
            ViewThatFits(in: .vertical) {
                post
                ScrollView { post }
            }
            .frame(maxHeight: Self.postHeight)
            .opacity(shown ? 1 : 0)
            .offset(y: shown ? 0 : 10)

            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "sparkle")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(Theme.accent)
                    .symbolEffect(.bounce, value: focus)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 6) {
                    if let reason = item.gist?.reason, !reason.isEmpty {
                        Text(reason).foregroundStyle(Theme.ink)
                    }
                    Text(consequence(of: focus))
                        .foregroundStyle(Theme.quiet)
                        .id(focus)
                        .transition(.opacity.combined(with: .offset(y: 4)))
                }
                .font(.system(size: 14))
                .fixedSize(horizontal: false, vertical: true)
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.soft, in: .rect(cornerRadius: 18, style: .continuous))
            .animation(.easeOut(duration: 0.18), value: focus)
            .opacity(shown ? 1 : 0)

            HStack(spacing: 10) {
                ForEach([Decision.reject, .read, .doIt], id: \.self) { decision in
                    choice(decision)
                }
            }
        }
        .padding(20)
        .background(Theme.tray, in: .rect(cornerRadius: 34, style: .continuous))
        .shadow(color: .black.opacity(0.12), radius: 24, y: 12)
        .onAppear { withAnimation(.spring(duration: 0.45).delay(0.06)) { shown = true } }
    }

    private var post: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(item.headline)
                .font(.system(size: 20, weight: .bold))
                .kerning(-0.3)
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            PostView(item: item)
        }
    }

    private func choice(_ decision: Decision) -> some View {
        let primary = decision == item.suggested
        return Button {
            decide(decision)
        } label: {
            Text(decision.label(for: item))
                .font(.system(size: 16, weight: .semibold))
                .frame(minWidth: 0, maxWidth: .infinity)
                .frame(height: 50)
                .foregroundStyle(primary ? .white : Theme.ink)
                .background(primary ? Theme.ink : Theme.soft, in: .rect(cornerRadius: 16, style: .continuous))
                .contentShape(.rect)
        }
        .buttonStyle(PressScale())
        .onHover { over in hovered = over ? decision : (hovered == decision ? nil : hovered) }
        .help(Self.key(decision))
    }

    private func consequence(of decision: Decision) -> String {
        let name = item.bestProject?.name ?? "the project"
        switch decision {
        case .doIt where item.isArea: return "Keep: it goes on your \(name) shelf in Library, and a follow-up lands in Briefs after the Monday run."
        case .doIt: return "Do: the \(name) agent plans it with your other Do picks. The plan lands in Briefs in about 2 minutes."
        case .read: return "Read: saved to Readwise Reader. No agent runs."
        case .reject: return "Pass: it leaves Triage. \(undoHint) brings it back."
        }
    }

    private static func key(_ decision: Decision) -> String {
        switch decision {
        case .doIt: "D or K"
        case .read: "R"
        case .reject: "X"
        }
    }

    #if os(macOS)
    static let postHeight: CGFloat = 520
    #else
    static let postHeight: CGFloat = 400
    #endif
}

/// A quick squeeze on press, the only motion a frequent action gets.
struct PressScale: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.95 : 1)
            .animation(.spring(duration: 0.2, bounce: 0.4), value: configuration.isPressed)
    }
}
