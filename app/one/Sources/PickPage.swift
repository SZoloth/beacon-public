import SwiftUI

/// Pick: check Beacon's calls one save at a time ("Keep or pass?", Beacon's call hidden), then two pairs,
/// then a portrait of what the session showed. A disagreement opens a tray under the card.
/// Mac keys: K keep, P pass, Space can't tell, ⌘Z undo; in the tray, Return or Space moves on.
struct PickPage: View {
    let model: PickModel
    var active = true
    @FocusState private var focused: Bool
    @State private var tickShown = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                header
                content
                    .padding(.top, 24)
            }
            .padding(.horizontal, TodayView.gutter)
            .padding(.bottom, 40)
            .frame(maxWidth: 820, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .scrollContentBackground(.hidden)
        .background(Theme.page)
        .task(id: active) { if active { await model.load() } }
        .onChange(of: model.agreements) { flashTick() }
        #if os(iOS)
        .safeAreaInset(edge: .bottom) {
            if case .card = model.step { thumbBar }
        }
        #endif
        #if os(macOS)
        .focusable()
        .focused($focused)
        .focusEffectDisabled()
        .onChange(of: active, initial: true) { focused = active }
        .onKeyPress(phases: .down) { press in key(press) }
        #endif
    }

    // MARK: Header

    private var header: some View {
        HStack(alignment: .lastTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Pick")
                    .font(.system(size: TodayView.titleSize, weight: .heavy))
                    .kerning(-1.2)
                    .foregroundStyle(Theme.ink)
                Text("Check Beacon's calls on your saves. It learns from every answer.")
                    .font(.system(size: 14))
                    .foregroundStyle(Theme.quiet)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            if model.cardsTotal > 0, !isPortrait {
                HStack(spacing: 6) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundStyle(Theme.accent)
                        .opacity(tickShown ? 1 : 0)
                        .scaleEffect(tickShown ? 1 : 0.5)
                        .accessibilityHidden(true)
                    Text("\(cardNumber) of \(model.cardsTotal)")
                        .font(.system(size: 15, weight: .semibold).monospacedDigit())
                        .contentTransition(.numericText())
                        .foregroundStyle(Theme.quiet)
                }
                .animation(.spring(duration: 0.3), value: model.cardsDone)
            }
        }
        .padding(.top, TodayView.headerTop)
    }

    private var isPortrait: Bool { model.step == .end || model.step == .resting }

    /// The card on screen, or in the tray; the last card once the pairs start.
    private var cardNumber: Int {
        if case .card = model.step { return min(model.cardsDone + 1, model.cardsTotal) }
        return max(model.cardsDone, 1)
    }

    // MARK: Steps

    @ViewBuilder private var content: some View {
        switch model.step {
        case .loading:
            ProgressView().frame(maxWidth: .infinity).padding(.top, 60)
        case .failed(let message):
            VStack(alignment: .leading, spacing: 12) {
                Text(message).font(.system(size: 15)).foregroundStyle(Theme.quiet).fixedSize(horizontal: false, vertical: true)
                Button { Task { await model.start(new: false) } } label: { BigLabel(title: "Try again", primary: true) }
                    .buttonStyle(PressScale())
                    .frame(maxWidth: 220)
            }
            .padding(.top, 24)
        case .card(let card):
            cardStep(card)
        case .tray(let tray):
            trayStep(tray)
        case .pair(let pair):
            PairStep(pair: pair, reveal: nil, model: model)
                .id(pair.position)
                .transition(.opacity.combined(with: .offset(y: 16)))
        case .reveal(let reveal):
            PairStep(pair: reveal.pair, reveal: reveal, model: model)
                .id(reveal.pair.position)
                .task {
                    try? await Task.sleep(for: .seconds(1.6))
                    await model.finishReveal()
                }
        case .end:
            Portrait(model: model)
        case .resting:
            resting
        }
    }

    private func cardStep(_ card: CallSession.Card) -> some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Keep or pass?")
                .font(.system(size: 22, weight: .bold))
                .kerning(-0.3)
                .foregroundStyle(Theme.ink)
            SaveCard(item: card.item)
                .id(card.id)
                .transition(.asymmetric(insertion: .opacity.combined(with: .offset(y: 16)), removal: leaving))
            #if os(macOS)
            macAnswers
            #endif
        }
        .animation(.spring(duration: 0.35, bounce: 0.15), value: card.id)
    }

    /// The card leaves the way you sent it: a keep to the right, a pass to the left.
    private var leaving: AnyTransition {
        guard !reduceMotion else { return .opacity }
        return switch model.lastAnswer {
        case .keep: .opacity.combined(with: .offset(x: 60))
        case .pass: .opacity.combined(with: .offset(x: -60))
        default: .opacity
        }
    }

    private func trayStep(_ tray: PickModel.Tray) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            SaveCard(item: tray.card.item, compact: true)
                .opacity(0.7)
            DisagreementTray(tray: tray, model: model)
                .id(tray.card.id)
                .transition(reduceMotion ? .opacity : .move(edge: .bottom).combined(with: .opacity))
        }
        .animation(.spring(duration: 0.4, bounce: 0.12), value: tray.card.id)
    }

    #if os(macOS)
    /// Pass and Keep as the two real answers; Can't tell and Undo quiet beside them.
    private var macAnswers: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 12) {
                Button { Task { await model.answer(.pass) } } label: { BigLabel(title: "Pass", primary: true) }
                    .buttonStyle(PressScale())
                    .frame(maxWidth: 180)
                Button { Task { await model.answer(.keep) } } label: { BigLabel(title: "Keep", primary: true) }
                    .buttonStyle(PressScale())
                    .frame(maxWidth: 180)
                Button("Can't tell") { Task { await model.answer(.unsure) } }
                    .buttonStyle(.plain)
                    .padding(.leading, 8)
                Button("Undo") { Task { await model.undo() } }
                    .buttonStyle(.plain)
                    .disabled(!model.canUndo)
                    .opacity(model.canUndo ? 1 : 0.4)
                    .padding(.leading, 8)
            }
            .font(.system(size: 15, weight: .semibold))
            .foregroundStyle(Theme.quiet)
            Text("K keep · P pass · Space can't tell · ⌘Z undo")
                .font(.system(size: 12))
                .foregroundStyle(Theme.quiet)
        }
        .disabled(model.busy)
    }

    private func key(_ press: KeyPress) -> KeyPress.Result {
        if press.modifiers.contains(.command) {
            guard press.characters == "z" else { return .ignored }
            Task { await model.undo() }
            return .handled
        }
        switch model.step {
        case .card:
            switch press.characters.lowercased() {
            case "k": Task { await model.answer(.keep) }
            case "p": Task { await model.answer(.pass) }
            case " ": Task { await model.answer(.unsure) }
            default: return .ignored
            }
        case .tray:
            guard press.key == .return || press.key == .space else { return .ignored }
            Task { await model.continueFromTray() }
        default:
            return .ignored
        }
        return .handled
    }
    #endif

    #if os(iOS)
    /// Keep and Pass at thumb height; Can't tell and Undo quieter above them.
    private var thumbBar: some View {
        VStack(spacing: 10) {
            HStack {
                Button("Undo") { Task { await model.undo() } }
                    .disabled(!model.canUndo)
                Spacer()
                Button("Can't tell") { Task { await model.answer(.unsure) } }
            }
            .font(.system(size: 15, weight: .semibold))
            .foregroundStyle(Theme.quiet)
            HStack(spacing: 12) {
                Button { Task { await model.answer(.pass) } } label: { ThumbLabel(title: "Pass") }
                    .buttonStyle(PressScale())
                Button { Task { await model.answer(.keep) } } label: { ThumbLabel(title: "Keep") }
                    .buttonStyle(PressScale())
            }
        }
        .disabled(model.busy)
        .padding(.horizontal, 20)
        .padding(.top, 10)
        .padding(.bottom, 8)
        .background(Theme.page)
    }
    #endif

    private var resting: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Done for today")
                .font(.system(size: 22, weight: .bold))
                .foregroundStyle(Theme.ink)
            Button("10 more") { Task { await model.start() } }
                .buttonStyle(.plain)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Theme.quiet)
        }
        .padding(.top, 24)
    }

    private func flashTick() {
        withAnimation(.spring(duration: 0.25, bounce: 0.4)) {
            tickShown = true
        } completion: {
            withAnimation(.easeOut(duration: 0.3).delay(0.45)) { tickShown = false }
        }
    }
}

// MARK: - A save

/// One save, as the card shows it: author, text and its first picture, nothing of Beacon's call.
private struct SaveCard: View {
    let item: CallSession.Item
    var compact = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let author = item.author, !author.isEmpty {
                Text(author)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Theme.quiet)
            }
            if !item.text.isEmpty {
                Text(item.text)
                    .font(.system(size: compact ? 15 : 17))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(compact ? 3 : 14)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !compact, let picture = item.picture {
                AsyncImage(url: picture) { image in
                    image.resizable().scaledToFit()
                } placeholder: {
                    Theme.soft
                }
                .frame(maxWidth: .infinity, maxHeight: 300)
                .clipShape(.rect(cornerRadius: 14, style: .continuous))
                .accessibilityHidden(true)
            }
        }
        .padding(compact ? 16 : 22)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(Theme.tray, in: .rect(cornerRadius: 26, style: .continuous))
        .shadow(color: .black.opacity(0.06), radius: 10, y: 4)
        .contextMenu {
            if let link = item.link { Button("Open the post") { _ = Platform.open(link) } }
        }
    }
}

// MARK: - The disagreement tray

private struct DisagreementTray: View {
    let tray: PickModel.Tray
    let model: PickModel
    @State private var askingWhy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(tray.headline)
                .font(.system(size: 20, weight: .bold))
                .kerning(-0.3)
                .foregroundStyle(Theme.ink)
            if let reason = tray.reason {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "sparkle")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Theme.accent)
                    Text(reason)
                        .font(.system(size: 14))
                        .foregroundStyle(Theme.quiet)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if tray.answer == .keep { destination }
            if !tray.chips.isEmpty { chips }
            HStack(spacing: 10) {
                Button { Task { await model.undo() } } label: { BigLabel(title: "Undo", primary: false) }
                    .buttonStyle(PressScale())
                    .help("⌘Z")
                Button { Task { await model.continueFromTray() } } label: { BigLabel(title: "Next", primary: true) }
                    .buttonStyle(PressScale())
                    .help("Return")
            }
            .disabled(model.busy)
            .frame(maxWidth: 420)
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.tray, in: .rect(cornerRadius: 30, style: .continuous))
        .shadow(color: .black.opacity(0.1), radius: 20, y: 10)
    }

    /// Where the keep goes; Beacon's suggestion first, any area after it.
    private var destination: some View {
        HStack(spacing: 10) {
            Text("Goes to")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(Theme.quiet)
            Menu {
                ForEach(choices, id: \.self) { slug in
                    Button(Self.name(slug)) { model.setDestination(slug) }
                }
            } label: {
                Group {
                    if let slug = tray.destination {
                        ShelfLabel(project: Project(slug: slug, name: Self.name(slug), fit: nil, kind: "area"))
                    } else {
                        Text("Choose a shelf")
                    }
                }
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                    .padding(.horizontal, 12)
                    .frame(minHeight: 30)
                    .background(Theme.control, in: .capsule)
            }
            .menuStyle(.button)
            .buttonStyle(.plain)
            .fixedSize()
        }
    }

    private var choices: [String] {
        var slugs = tray.destinations
        for area in Theme.areas where !slugs.contains(area.slug) { slugs.append(area.slug) }
        return slugs
    }

    static func name(_ slug: String) -> String {
        Theme.areas.first { $0.slug == slug }?.name ?? slug.split(separator: "-").map(\.capitalized).joined(separator: " ")
    }

    /// Closed until you ask: most changes need no reason.
    @ViewBuilder private var chips: some View {
        if !askingWhy {
            Button("Say why") { withAnimation(.spring(duration: 0.3)) { askingWhy = true } }
                .buttonStyle(.plain)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Theme.accent)
        } else {
            ChipFlow(spacing: 8) {
                ForEach(tray.chips, id: \.self) { chip in
                    let on = tray.chip == chip
                    Button { model.setChip(chip) } label: {
                        Text(chip)
                            .font(.system(size: 13, weight: .semibold))
                            .padding(.horizontal, 12)
                            .frame(minHeight: 30)
                            .foregroundStyle(on ? .white : Theme.ink)
                            .background(on ? Theme.ink : Theme.control, in: .capsule)
                    }
                    .buttonStyle(PressScale())
                    .accessibilityAddTraits(on ? .isSelected : [])
                }
            }
            .transition(.opacity)
        }
    }
}

/// Chips in rows that wrap.
private struct ChipFlow: Layout {
    var spacing: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(width: proposal.width ?? .infinity, subviews: subviews)
        return CGSize(width: rows.map(\.width).max() ?? 0, height: rows.last.map { $0.y + $0.height } ?? 0)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        for row in arrange(width: bounds.width, subviews: subviews) {
            var x = bounds.minX
            for index in row.indices {
                let size = subviews[index].sizeThatFits(.unspecified)
                subviews[index].place(at: CGPoint(x: x, y: bounds.minY + row.y), proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
        }
    }

    private struct Row { var indices: [Int] = []; var width: CGFloat = 0; var height: CGFloat = 0; var y: CGFloat = 0 }

    private func arrange(width: CGFloat, subviews: Subviews) -> [Row] {
        var rows = [Row()]
        for index in subviews.indices {
            let size = subviews[index].sizeThatFits(.unspecified)
            if !rows[rows.count - 1].indices.isEmpty, rows[rows.count - 1].width + spacing + size.width > width {
                let last = rows[rows.count - 1]
                rows.append(Row(y: last.y + last.height + spacing))
            }
            let gap = rows[rows.count - 1].indices.isEmpty ? 0 : spacing
            rows[rows.count - 1].indices.append(index)
            rows[rows.count - 1].width += gap + size.width
            rows[rows.count - 1].height = max(rows[rows.count - 1].height, size.height)
        }
        return rows
    }
}

// MARK: - Pairs

/// Two saves, the pair's own question, and "About the same". After your pick, Beacon's guess shows.
private struct PairStep: View {
    let pair: CallSession.Pair
    let reveal: PickModel.Reveal?
    let model: PickModel

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text(pair.question ?? "Which would you rather have saved?")
                .font(.system(size: 22, weight: .bold))
                .kerning(-0.3)
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            let layout = Self.sideBySide ? AnyLayout(HStackLayout(alignment: .top, spacing: 16)) : AnyLayout(VStackLayout(spacing: 12))
            layout {
                side(pair.left, .left)
                side(pair.right, .right)
            }
            if let reveal {
                Text(Self.guessLine(pair: pair, choice: reveal.choice))
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(Theme.quiet)
                    .transition(.opacity)
            } else {
                HStack(spacing: 10) {
                    Button { Task { await model.undo() } } label: { BigLabel(title: "Undo", primary: false) }
                        .buttonStyle(PressScale())
                        .disabled(!model.canUndo)
                        .opacity(model.canUndo ? 1 : 0.4)
                    Button { Task { await model.pick(.same) } } label: { BigLabel(title: "About the same", primary: false) }
                        .buttonStyle(PressScale())
                }
                .disabled(model.busy)
                .frame(maxWidth: 420)
            }
        }
        .animation(.spring(duration: 0.35), value: reveal)
    }

    private func side(_ save: CallSession.Pair.Side, _ choice: PickModel.PairChoice) -> some View {
        let chosen = reveal?.choice == choice
        let guessed = reveal != nil && pair.beaconGuess == choice.rawValue
        return Button { Task { await model.pick(choice) } } label: {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text(save.author ?? save.source ?? "")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Theme.quiet)
                    Spacer()
                    if guessed {
                        Label("Beacon's guess", systemImage: "sparkle")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(Theme.accent)
                            .transition(.scale(scale: 0.6).combined(with: .opacity))
                    }
                }
                Text(save.text)
                    .font(.system(size: 16))
                    .foregroundStyle(Theme.ink)
                    .multilineTextAlignment(.leading)
                    .lineLimit(10)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(20)
            .frame(maxWidth: .infinity, minHeight: 150, alignment: .topLeading)
            .background(Theme.tray, in: .rect(cornerRadius: 26, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 26, style: .continuous)
                    .strokeBorder(Theme.ink, lineWidth: chosen ? 2 : 0)
            }
            .shadow(color: .black.opacity(0.06), radius: 10, y: 4)
            .contentShape(.rect(cornerRadius: 26))
        }
        .buttonStyle(PressScale())
        .disabled(reveal != nil || model.busy)
        .accessibilityLabel("\(choice == .left ? "First" : "Second"): \(save.author ?? ""). \(save.text)")
    }

    /// After a pick: whether Beacon guessed the same.
    static func guessLine(pair: CallSession.Pair, choice: PickModel.PairChoice) -> String {
        switch pair.beaconGuess {
        case "left", "right":
            if choice == .same { return "Beacon guessed one of them." }
            return pair.beaconGuess == choice.rawValue ? "Beacon guessed the same." : "Beacon guessed the other one."
        default:
            return "Beacon had no guess for this pair."
        }
    }

    #if os(macOS)
    static let sideBySide = true
    #else
    static let sideBySide = false
    #endif
}

// MARK: - The portrait

/// Where a session ends: what you checked, how Beacon's calls stand, and your taste so far.
private struct Portrait: View {
    let model: PickModel
    @State private var shown = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            if let summary = model.summary {
                HStack(spacing: 10) {
                    Image(systemName: "sparkle")
                        .font(.system(size: 17, weight: .bold))
                        .foregroundStyle(Theme.accent)
                        .symbolEffect(.bounce, value: shown)
                    Text("\(summary.session.checked) checked, \(summary.session.changed) changed")
                        .font(.system(size: 26, weight: .heavy))
                        .kerning(-0.5)
                        .foregroundStyle(Theme.ink)
                }
                section("Beacon's calls", index: 0) {
                    ForEach(summary.groups, id: \.name) { group in
                        row(group.label, detail: group.line)
                    }
                    if summary.flaggedNext > 0 {
                        Text("Your changes flagged \(summary.flaggedNext) similar calls; they lead your next session.")
                            .font(.system(size: 13))
                            .foregroundStyle(Theme.quiet)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let offer = summary.offer { OfferRow(offer: offer, model: model) }
                }
                let taste = summary.portraitAxes
                if !taste.shown.isEmpty {
                    section("Your taste", index: 1) {
                        ForEach(taste.shown, id: \.question) { axis in AxisRow(axis: axis) }
                        if taste.later > 0 {
                            Text("\(taste.later) more \(taste.later == 1 ? "question" : "questions") after this")
                                .font(.system(size: 13))
                                .foregroundStyle(Theme.quiet)
                        }
                    }
                }
            }
            if !model.changes.isEmpty {
                section("You changed", index: 2) {
                    ForEach(model.changes) { change in changeRow(change) }
                }
            }
            HStack(spacing: 18) {
                Button { model.doneForToday() } label: { BigLabel(title: "Done for today", primary: true) }
                    .buttonStyle(PressScale())
                    .frame(maxWidth: 240)
                Button("10 more") { Task { await model.start() } }
                    .buttonStyle(.plain)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Theme.quiet)
            }
            .opacity(shown ? 1 : 0)
            .animation(.easeOut(duration: 0.3).delay(0.45), value: shown)
        }
        .padding(24)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.tray, in: .rect(cornerRadius: 34, style: .continuous))
        .shadow(color: .black.opacity(0.1), radius: 24, y: 12)
        .onAppear { shown = true }
    }

    /// One section, rising in after the one before it.
    private func section<Rows: View>(_ title: String, index: Int, @ViewBuilder rows: () -> Rows) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title.uppercased())
                .font(.system(size: 12, weight: .semibold))
                .kerning(0.8)
                .foregroundStyle(Theme.quiet)
            rows()
        }
        .opacity(shown ? 1 : 0)
        .offset(y: shown || reduceMotion ? 0 : 12)
        .animation(.spring(duration: 0.5, bounce: 0.2).delay(0.12 * Double(index + 1)), value: shown)
    }

    private func row(_ title: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.ink)
            Text(detail).font(.system(size: 14).monospacedDigit()).foregroundStyle(Theme.quiet)
        }
    }

    private func changeRow(_ change: PickModel.Change) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(change.answer.label)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Theme.ink)
                .frame(width: 44, alignment: .leading)
            Text(change.item.text.isEmpty ? (change.item.author ?? "") : change.item.text)
                .font(.system(size: 14))
                .foregroundStyle(change.undone ? Theme.quiet : Theme.ink)
                .strikethrough(change.undone)
                .lineLimit(1)
            Spacer(minLength: 8)
            if change.undone {
                Text("Undone").font(.system(size: 13)).foregroundStyle(Theme.quiet)
            } else {
                Button("Undo") { Task { await model.undoChange(change) } }
                    .buttonStyle(.plain)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Theme.accent)
                    .disabled(model.busy)
            }
        }
    }
}

/// A group Beacon often gets wrong: re-deciding it costs money, so it waits for your tap.
private struct OfferRow: View {
    let offer: CallSummary.Offer
    let model: PickModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(model.offerAccepted
                 ? "Beacon is re-deciding them with your checks as examples."
                 : "Beacon gets \((offer.label ?? offer.group).lowercased()) wrong often. Re-decide \(offer.items.map { "all \($0.formatted())" } ?? "them") with your checks as examples?")
                .font(.system(size: 14))
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            if !model.offerAccepted {
                Button { Task { await model.acceptOffer() } } label: {
                    Text("Re-decide · about \(offer.costUsd, format: .currency(code: "USD"))")
                        .font(.system(size: 14, weight: .semibold))
                        .padding(.horizontal, 14)
                        .frame(minHeight: 34)
                        .foregroundStyle(Theme.ink)
                        .background(Theme.control, in: .capsule)
                }
                .buttonStyle(PressScale())
                .disabled(model.busy)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.soft, in: .rect(cornerRadius: 18, style: .continuous))
    }
}

/// One taste axis: a marker on a line once there are enough decisive picks, else how many more it needs.
private struct AxisRow: View {
    let axis: CallSummary.Axis

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(axis.question)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            if axis.showsMarker, let lean = axis.lean {
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.control).frame(height: 4)
                    Circle()
                        .fill(Theme.accent)
                        .frame(width: 12, height: 12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .visualEffect { content, proxy in
                            content.offset(x: max(0, min(proxy.size.width - 12, proxy.size.width * lean - 6)))
                        }
                }
                .frame(height: 14)
                .frame(maxWidth: 360)
                .accessibilityLabel("Leans \(Int((lean * 100).rounded()))% toward Beacon's guess")
            }
            Text(detail)
                .font(.system(size: 14).monospacedDigit())
                .foregroundStyle(Theme.quiet)
        }
    }

    private var detail: String {
        var parts: [String] = []
        if !axis.showsMarker {
            parts.append("Needs \(axis.needed) more \(axis.needed == 1 ? "pick" : "picks")")
        } else if let state = axis.state {
            parts.append("\(state.prefix(1).uppercased())\(state.dropFirst()) · \(axis.decisive) picks")
        }
        if axis.said > 0 { parts.append("you gave it as a reason \(axis.said) \(axis.said == 1 ? "time" : "times")") }
        return parts.joined(separator: " · ")
    }
}

// MARK: - Buttons

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
            .foregroundStyle(primary ? Theme.page : Theme.ink)
            .background(primary ? Theme.ink : Theme.soft, in: .rect(cornerRadius: 16, style: .continuous))
            .contentShape(.rect)
    }
}

#if os(iOS)
/// Keep and Pass on the phone: tall enough to hit without looking.
private struct ThumbLabel: View {
    let title: String

    var body: some View {
        Text(title)
            .font(.system(size: 19, weight: .bold))
            .frame(maxWidth: .infinity)
            .frame(height: 64)
            .foregroundStyle(Theme.page)
            .background(Theme.ink, in: .rect(cornerRadius: 20, style: .continuous))
            .contentShape(.rect)
    }
}
#endif
