import SwiftUI

/// Pick, in Today's language: two of your own saves side by side (stacked on the iPhone), one question,
/// and a tap on the one you'd rather have saved. Neither, Can't tell and Back sit under them; a chip says why.
/// Mac keys: ← and → pick a side, ↓ neither, Space can't tell, Delete goes back.
struct PickPage: View {
    let model: PickModel
    var active = true
    @FocusState private var focused: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                header
                content
                    .padding(.top, 24)
            }
            .padding(.horizontal, TodayView.gutter)
            .padding(.bottom, 40)
            .frame(maxWidth: 1100, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .scrollContentBackground(.hidden)
        .background(Theme.page)
        .refreshable { await model.load() }
        .task(id: active) { if active { await model.load() } }
        #if os(macOS)
        .focusable()
        .focused($focused)
        .focusEffectDisabled()
        .onChange(of: active, initial: true) { focused = active }
        .onKeyPress(keys: [.leftArrow, .rightArrow, .downArrow, .space, .delete]) { press in
            switch press.key {
            case .leftArrow: model.choose(.left)
            case .rightArrow: model.choose(.right)
            case .downArrow: model.choose(.neither)
            case .space: model.choose(.skip)
            default: Task { await model.back() }
            }
            return .handled
        }
        #endif
    }

    private var header: some View {
        HStack(alignment: .lastTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Which would you rather have saved?")
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(Theme.quiet)
                Text("Pick")
                    .font(.system(size: TodayView.titleSize, weight: .heavy))
                    .kerning(-1.2)
                    .foregroundStyle(Theme.ink)
            }
            Spacer()
            if model.total > 0 {
                Text("\(model.done) of \(model.total)")
                    .font(.system(size: 15, weight: .semibold).monospacedDigit())
                    .contentTransition(.numericText())
                    .foregroundStyle(Theme.quiet)
                    .animation(.spring(duration: 0.3), value: model.done)
            }
        }
        .padding(.top, TodayView.headerTop)
    }

    @ViewBuilder private var content: some View {
        if let pair = model.current, let round = model.round {
            VStack(alignment: .leading, spacing: 20) {
                if let question = round.question {
                    Text(question)
                        .font(.system(size: 22, weight: .bold))
                        .kerning(-0.3)
                        .foregroundStyle(Theme.ink)
                }
                cards(pair)
                    .id(pair.position)
                    .transition(.asymmetric(insertion: .opacity.combined(with: .offset(y: 16)), removal: .opacity))
                if let chips = round.chips, !chips.isEmpty { chipRow(chips) }
                controls
            }
            .animation(.spring(duration: 0.35, bounce: 0.15), value: pair.position)
        } else if model.isPreparing {
            Waiting(title: "Building the next round",
                    detail: "Beacon matches your saves by topic on this Mac. It takes a few minutes.")
        } else if model.isFinished {
            finished
        } else {
            Waiting(title: "No round yet", detail: Self.noRound(model.round?.reason), busy: false) {
                Button { Task { await model.prepare() } } label: { BigLabel(title: "Build a round", primary: true) }
                    .buttonStyle(PressScale())
                    .frame(maxWidth: 260)
            }
        }
    }

    @ViewBuilder private func cards(_ pair: PickRound.Pair) -> some View {
        let layout = Self.sideBySide ? AnyLayout(HStackLayout(alignment: .top, spacing: 16)) : AnyLayout(VStackLayout(spacing: 12))
        layout {
            PickCard(card: pair.left, side: "Left") { model.choose(.left) }
            PickCard(card: pair.right, side: "Right") { model.choose(.right) }
        }
    }

    private func chipRow(_ chips: [String]) -> some View {
        HStack(spacing: 8) {
            Text("Why, if you know:")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(Theme.quiet)
            ForEach(chips, id: \.self) { chip in
                let on = model.why == chip
                Button { model.why = on ? nil : chip } label: {
                    Text(chip)
                        .font(.system(size: 13, weight: .semibold))
                        .padding(.horizontal, 12)
                        .frame(minHeight: 28)
                        .foregroundStyle(on ? .white : Theme.ink)
                        .background(on ? Theme.ink : Theme.control, in: .capsule)
                }
                .buttonStyle(PressScale())
                .accessibilityAddTraits(on ? .isSelected : [])
            }
        }
    }

    private var controls: some View {
        HStack(spacing: 10) {
            Button { Task { await model.back() } } label: { BigLabel(title: "Back", primary: false) }
                .buttonStyle(PressScale())
                .disabled(model.done == 0)
                .opacity(model.done == 0 ? 0.4 : 1)
            Button { model.choose(.neither) } label: { BigLabel(title: "Neither", primary: false) }
                .buttonStyle(PressScale())
                .help("Both are a pass")
            Button { model.choose(.skip) } label: { BigLabel(title: "Can't tell", primary: false) }
                .buttonStyle(PressScale())
        }
        .frame(maxWidth: 560)
    }

    private var finished: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Round done").font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.ink)
            if let axis = model.round?.axis, let lean = model.summary?.axes[axis] {
                VStack(alignment: .leading, spacing: 6) {
                    Text(lean.question).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.ink)
                    Text(Self.leanLine(lean)).font(.system(size: 14)).foregroundStyle(Theme.quiet)
                }
                .padding(16)
                .frame(maxWidth: 560, alignment: .leading)
                .background(Theme.tray, in: .rect(cornerRadius: 20, style: .continuous))
            }
            Button { Task { await model.prepare() } } label: { BigLabel(title: "Next round", primary: true) }
                .buttonStyle(PressScale())
                .frame(maxWidth: 260)
        }
    }

    /// "Leaning · you chose Beacon's guess 9 of 12 times, and agreed with yourself on 2 of 2 repeats."
    static func leanLine(_ lean: PickSummary.Axis) -> String {
        let state = lean.state.prefix(1).uppercased() + lean.state.dropFirst()
        guard lean.decisive > 0 else { return "\(state) · no decisive picks yet" }
        var line = "\(state) · \(lean.towardKeep) of \(lean.decisive) decisive picks went the way Beacon guessed"
        if let consistency = lean.consistency { line += ", \(Int((consistency * 100).rounded()))% the same on repeats" }
        return line
    }

    /// Beacon's reason, unless it's the plain "none prepared", which tells you to run a command the button runs.
    static func noRound(_ reason: String?) -> String {
        guard let reason else { return "Beacon hasn't answered yet." }
        return reason.hasPrefix("No round prepared")
            ? "Build one here. Beacon matches your saves by topic on this Mac, which takes a few minutes." : reason
    }

    #if os(macOS)
    static let sideBySide = true
    #else
    static let sideBySide = false
    #endif
}

/// One save, as a tray you tap to pick it.
private struct PickCard: View {
    let card: PickRound.Card
    let side: String
    let choose: () -> Void

    var body: some View {
        Button(action: choose) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 6) {
                    Text(card.author ?? card.source)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Theme.quiet)
                    if card.hasMedia {
                        Image(systemName: "photo")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(Theme.quiet)
                            .accessibilityLabel("Has pictures")
                    }
                }
                Text(card.text)
                    .font(.system(size: 16))
                    .foregroundStyle(Theme.ink)
                    .multilineTextAlignment(.leading)
                    .lineLimit(12)
                    .fixedSize(horizontal: false, vertical: true)
                if let takeaway = card.takeaway, !takeaway.isEmpty {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: "sparkle")
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(Theme.accent)
                        Text(takeaway)
                            .font(.system(size: 13))
                            .foregroundStyle(Theme.quiet)
                            .multilineTextAlignment(.leading)
                    }
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, minHeight: 180, alignment: .topLeading)
            .background(Theme.tray, in: .rect(cornerRadius: 26, style: .continuous))
            .shadow(color: .black.opacity(0.06), radius: 10, y: 4)
            .contentShape(.rect(cornerRadius: 26))
        }
        .buttonStyle(PressScale())
        .accessibilityLabel("\(side): \(card.author ?? card.source). \(card.text)")
        .accessibilityHint("Picks this save")
        .contextMenu {
            if let link = card.link { Button("Open the post") { _ = Platform.open(link) } }
        }
    }
}

/// No pair to show: what's happening and, when there's something to do, the button for it.
private struct Waiting<Action: View>: View {
    let title: String
    let detail: String
    var busy = true
    @ViewBuilder var action: Action

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: "sparkle")
                    .font(.system(size: 15, weight: .bold))
                    .foregroundStyle(Theme.accent)
                    .symbolEffect(.rotate, options: .speed(0.4).repeating, isActive: busy)
                Text(title).font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.ink)
            }
            Text(detail).font(.system(size: 15)).foregroundStyle(Theme.quiet).fixedSize(horizontal: false, vertical: true)
            action
        }
        .padding(.top, 40)
    }
}

extension Waiting where Action == EmptyView {
    init(title: String, detail: String, busy: Bool = true) {
        self.init(title: title, detail: detail, busy: busy) { EmptyView() }
    }
}

/// Triage's big buttons.
private struct BigLabel: View {
    let title: String
    let primary: Bool

    var body: some View {
        Text(title)
            .font(.system(size: 16, weight: .semibold))
            .lineLimit(1)
            .minimumScaleFactor(0.8)
            .frame(maxWidth: .infinity)
            .frame(height: 50)
            .foregroundStyle(primary ? .white : Theme.ink)
            .background(primary ? Theme.ink : Theme.soft, in: .rect(cornerRadius: 16, style: .continuous))
            .contentShape(.rect)
    }
}
