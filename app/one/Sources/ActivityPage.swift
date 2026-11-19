import SwiftUI

/// Activity (Briefs before): every hand-off to an agent and the plans waiting to start, in Triage's language.
/// What needs you comes first (a question, a hand-off to send, work to review), then the plans to start,
/// then what's running and what finished. A row opens a tray with everything about it and its actions:
/// a pane beside the list on the Mac, rising from below on the iPhone. Nothing starts but a tap on Start.
struct ActivityPage: View {
    @Bindable var store: TriageStore
    let activity: ActivityModel
    var active = true
    @FocusState private var focused: Bool
    @State private var selectedID: String?
    /// Briefs playing their exit: Done shows a check and slides right, Dismiss slides left.
    @State private var leaving: [String: Exit] = [:]
    @State private var travel = 1

    enum Exit { case done, dismissed }

    private struct BriefGroup: Identifiable {
        let id: String
        let title: String
        let slug: String?
        let summary: String
        let briefs: [Brief]
    }

    /// A row in the list: a hand-off, or a plan waiting to start.
    private enum Row: Identifiable, Equatable {
        case brief(Brief)
        case handOff(Activation)

        var id: String {
            switch self {
            case .brief(let brief): "b\(brief.id)"
            case .handOff(let record): "a\(record.id)"
            }
        }
    }

    /// Briefs flagged on the phone come first, then one group per project or area, in the agent's order.
    /// A brief whose save went to an agent leaves at once; the hand-off shows instead.
    private var groups: [BriefGroup] {
        let open = { (brief: Brief) in !activity.isStarted(itemId: brief.itemId) }
        let flagged = store.plans.flatMap(\.briefs).filter { $0.forMac == true && open($0) }
        let mac = flagged.isEmpty ? [] : [BriefGroup(id: "mac", title: "Start on the Mac", slug: nil, summary: "", briefs: flagged)]
        return mac + store.plans.compactMap { plan in
            let briefs = plan.briefs.filter { $0.forMac != true && open($0) }
            return briefs.isEmpty ? nil : BriefGroup(id: plan.workstream, title: plan.projectName, slug: plan.workstream,
                                                summary: plan.summary, briefs: briefs)
        }
    }

    /// Hand-off sections above the plans (what needs you) and below them (running and finished).
    private var upper: [ActivityModel.Section] { activity.sections.filter { [.needsYou, .readyToReview].contains($0.kind) } }
    private var lower: [ActivityModel.Section] { activity.sections.filter { ![.needsYou, .readyToReview].contains($0.kind) } }

    /// Every row in screen order, for ↑ ↓ and the tray.
    private var rows: [Row] {
        upper.flatMap { $0.records.map(Row.handOff) } + groups.flatMap { $0.briefs.map(Row.brief) }
            + lower.flatMap { $0.records.map(Row.handOff) }
    }
    private var selected: Row? { rows.first { $0.id == selectedID } }

    var body: some View {
        #if os(macOS)
        HStack(spacing: 0) {
            list.frame(minWidth: 460, idealWidth: 580, maxWidth: 620)
            ZStack(alignment: .top) {
                Theme.deep.ignoresSafeArea()
                if let row = selected ?? rows.first {
                    tray(row, close: nil)
                        .frame(maxWidth: 560)
                        .padding(.top, 70)
                        .padding(.horizontal, 32)
                        .padding(.bottom, 24)
                        .id(row.id)
                        .transition(.asymmetric(
                            insertion: .offset(y: CGFloat(travel) * 60).combined(with: .opacity),
                            removal: .offset(y: CGFloat(-travel) * 40).combined(with: .opacity)))
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
        .task(id: active) { if active { await activity.load() } }
        #else
        ZStack(alignment: .bottom) {
            list
            if let row = selected {
                Color.black.opacity(0.28).ignoresSafeArea()
                    .onTapGesture { selectedID = nil }
                    .transition(.opacity)
                tray(row) { selectedID = nil }
                    .padding(.horizontal, 8)
                    .padding(.bottom, 4)
                    .transition(.move(edge: .bottom).combined(with: .scale(scale: 0.96, anchor: .bottom)))
                    .zIndex(1)
            }
        }
        .animation(.spring(duration: 0.45, bounce: 0.22), value: selected?.id)
        .toolbar(selected == nil ? .visible : .hidden, for: .tabBar)
        .task { await activity.load() }
        #endif
    }

    @ViewBuilder
    private func tray(_ row: Row, close: (() -> Void)?) -> some View {
        switch row {
        case .brief(let brief):
            BriefTray(brief: brief, store: store, activity: activity, finish: { finish(brief, $0) }, close: close)
        case .handOff(let record):
            HandOffTray(record: record, activity: activity, close: close)
        }
    }

    private var list: some View {
        let groups = groups
        let highlighted = selected?.id ?? defaultSelection
        return ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                header
                handOffs(upper, highlighted: highlighted)
                let planning = store.planningRows
                if !planning.isEmpty {
                    SectionTitle(title: "Planning", count: planning.reduce(0) { $0 + $1.items.count })
                    ForEach(planning, id: \.project) { PlanningLine(project: $0.project, count: $0.items.count) }
                }
                if groups.isEmpty && planning.isEmpty && activity.records.isEmpty && !store.isLoading {
                    NothingPlanned().frame(maxWidth: .infinity).padding(.top, 80)
                }
                ForEach(groups) { group in
                    SectionTitle(title: group.title, count: group.briefs.count, slug: group.slug)
                    if !group.summary.isEmpty {
                        Text(group.summary)
                            .font(.system(size: 13))
                            .foregroundStyle(Theme.quiet)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.bottom, 4)
                    }
                    ForEach(group.briefs) { brief in
                        BriefRow(brief: brief, store: store, activity: activity, isSelected: Row.brief(brief).id == highlighted,
                                 done: leaving[brief.id] == .done, pass: { finish(brief, .dismissed) }) {
                            select(.brief(brief))
                        }
                        .transition(.asymmetric(insertion: .opacity, removal: exit(leaving[brief.id])))
                    }
                }
                handOffs(lower, highlighted: highlighted)
            }
            .padding(.horizontal, TodayView.gutter)
            .padding(.bottom, 40)
            .animation(.spring(duration: 0.42, bounce: 0.15), value: rows.map(\.id))
        }
        .scrollContentBackground(.hidden)
        .background(Theme.page)
        .refreshable {
            await store.loadPlans()
            await activity.load()
        }
    }

    @ViewBuilder
    private func handOffs(_ sections: [ActivityModel.Section], highlighted: String?) -> some View {
        ForEach(sections) { section in
            SectionTitle(title: section.kind.title, count: section.records.count)
            // Keyed by the whole record, so a row that changes state is drawn afresh: keyed by id alone, the
            // lazy stack kept a closed hand-off's old pill.
            ForEach(section.records, id: \.self) { record in
                HandOffRow(record: record, isSelected: Row.handOff(record).id == highlighted) { select(.handOff(record)) }
                    .transition(.opacity)
            }
        }
    }

    private var header: some View {
        let waiting = activity.toActOn(store.plans)
        return HStack(alignment: .lastTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text("What the agents are doing")
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(Theme.quiet)
                Text("Activity")
                    .font(.system(size: TodayView.titleSize, weight: .heavy))
                    .kerning(-1.2)
                    .foregroundStyle(Theme.ink)
            }
            Spacer()
            Text("\(waiting) to act on")
                .font(.system(size: 15, weight: .semibold).monospacedDigit())
                .contentTransition(.numericText(countsDown: true))
                .foregroundStyle(Theme.quiet)
        }
        .animation(.spring(duration: 0.4), value: waiting)
        .padding(.top, TodayView.headerTop)
    }

    private var defaultSelection: String? {
        #if os(macOS)
        rows.first?.id
        #else
        nil
        #endif
    }

    private func select(_ row: Row) {
        let all = rows
        travel = (all.firstIndex(of: row) ?? 0) >= (all.firstIndex { $0.id == selectedID } ?? 0) ? 1 : -1
        selectedID = row.id
    }

    private func exit(_ exit: Exit?) -> AnyTransition {
        switch exit {
        case .done: .move(edge: .trailing).combined(with: .scale(scale: 0.8, anchor: .trailing)).combined(with: .opacity)
        case .dismissed: .move(edge: .leading).combined(with: .opacity)
        case nil: .opacity
        }
    }

    /// The row shows a check for a beat (Done) or simply goes (Dismiss), and the next row takes the tray.
    private func finish(_ brief: Brief, _ how: Exit) {
        guard leaving[brief.id] == nil else { return }
        let all = rows
        let next = all.firstIndex(of: .brief(brief)).flatMap { all.indices.contains($0 + 1) ? all[$0 + 1] : nil }
        withAnimation(.spring(duration: 0.25, bounce: 0.4)) { leaving[brief.id] = how }
        #if os(iOS)
        selectedID = nil
        #endif
        Task {
            try? await Task.sleep(for: .milliseconds(how == .done ? 260 : 60))
            withAnimation(.spring(duration: 0.42, bounce: 0.15)) {
                how == .done ? store.complete(brief) : store.dismiss(brief)
                #if os(macOS)
                if selectedID == nil || selectedID == Row.brief(brief).id { travel = 1; selectedID = next?.id }
                #endif
            }
            leaving[brief.id] = nil
        }
    }

    private func move(by offset: Int) {
        let all = rows
        guard let current = selected ?? all.first, let index = all.firstIndex(of: current) else { return }
        travel = offset > 0 ? 1 : -1
        selectedID = all[min(max(index + offset, 0), all.count - 1)].id
    }
}

/// One brief: what to do, the plan's first step under it, and the one-tap action.
private struct BriefRow: View {
    let brief: Brief
    let store: TriageStore
    let activity: ActivityModel
    let isSelected: Bool
    let done: Bool
    let pass: () -> Void
    let open: () -> Void

    var body: some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(brief.headline)
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                if let line = brief.brief?.nextStep ?? readyLine {
                    Text(line)
                        .font(.system(size: 13))
                        .foregroundStyle(Theme.quiet)
                        .lineLimit(1)
                }
            }
            // A tap rather than a Button because the pill is a Button of its own (see TodayRow).
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(.isButton)
            .accessibilityHint("Shows the whole plan")
            .accessibilityAction { open() }
            Spacer(minLength: 8)
            pill
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

    /// A follow-up leads with the work already done, so the row reads as something to approve.
    private var readyLine: String? {
        guard let followUp = brief.followUp else { return nil }
        guard let draft = followUp.drafts.first else { return followUp.summary }
        return "Draft ready · " + (draft.to.map { "\(draft.kind.capitalized) to \($0)" } ?? draft.kind.capitalized)
    }

    @ViewBuilder private var pill: some View {
        if done {
            Image(systemName: "checkmark")
                .font(.system(size: 12, weight: .heavy))
                .foregroundStyle(.white)
                .frame(width: 26, height: 26)
                .background(Theme.ink, in: .circle)
                .accessibilityLabel("Done")
                .transition(.scale(scale: 0.3).combined(with: .opacity))
        } else if brief.nothingToDo {
            // The agent found nothing to do (skip, or reference only): one tap agrees, like accepting Beacon's suggestion in Triage.
            Button(action: pass) { Pill(title: "Pass", tint: nil) }
                .buttonStyle(PressScale())
                .help("The agent found nothing to do here; dismiss it")
        } else {
            // The one way a hand-off starts: Beacon gives the save and its plan to the destination's agent.
            Button { activity.start(itemId: brief.itemId, destination: brief.projectName) } label: {
                Pill(title: "Start", tint: Theme.color(slug: brief.workstream))
            }
            .buttonStyle(PressScale())
            .help(brief.startHelp)
        }
    }
}

/// Today's pill shape: an area's color when it has one, quiet otherwise, so twenty rows don't shout.
private struct Pill: View {
    let title: String
    let tint: Color?

    var body: some View {
        Text(title)
            .font(.system(size: 13, weight: .semibold))
            .padding(.horizontal, 13)
            .frame(minHeight: 26)
            .foregroundStyle(tint ?? Theme.ink)
            .background(tint.map { $0.opacity(0.16) } ?? Theme.control, in: .capsule)
    }
}

/// The whole plan, the drafts and every action, one brief at a time.
private struct BriefTray: View {
    let brief: Brief
    let store: TriageStore
    let activity: ActivityModel
    let finish: (ActivityPage.Exit) -> Void
    var close: (() -> Void)?
    @State private var shown = false
    @State private var copied: Int?
    /// Your edits to each draft, by index; Copy and Hand off use the edited words.
    @State private var edits: [Int: String] = [:]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 10) {
                ShelfLabel(project: Project(slug: brief.workstream, name: brief.projectName, fit: nil,
                                            kind: brief.followUp == nil ? nil : "area"))
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                if let body = brief.brief {
                    Text("\(body.verdict.capitalized) · \(body.effort)")
                        .font(.system(size: 13))
                        .foregroundStyle(Theme.quiet)
                        .lineLimit(1)
                }
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

            ViewThatFits(in: .vertical) {
                content
                ScrollView { content }
            }
            .frame(maxHeight: TrayView.postHeight)
            .opacity(shown ? 1 : 0)
            .offset(y: shown ? 0 : 10)

            actions
        }
        .padding(20)
        .background(Theme.tray, in: .rect(cornerRadius: 34, style: .continuous))
        .shadow(color: .black.opacity(0.12), radius: 24, y: 12)
        .onAppear { withAnimation(.spring(duration: 0.45).delay(0.06)) { shown = true } }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(brief.headline)
                .font(.system(size: 20, weight: .bold))
                .kerning(-0.3)
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            if let body = brief.brief { plan(body) }
            if let followUp = brief.followUp { followUpBody(followUp) }
        }
        .font(.system(size: 14))
        .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder private func plan(_ body: Brief.Body) -> some View {
        Text(body.summary).foregroundStyle(Theme.ink)
        Callout(lead: "Next step", text: body.nextStep)
        if !body.whyHere.isEmpty { Labeled(title: "Why here") { Text(body.whyHere) } }
        if !body.steps.isEmpty {
            Labeled(title: "Steps") {
                ForEach(Array(body.steps.enumerated()), id: \.offset) { Text("\($0.offset + 1). \($0.element)") }
            }
        }
        if !body.files.isEmpty {
            Labeled(title: "Files") {
                Text(body.files.map { ($0 as NSString).abbreviatingWithTildeInPath }.joined(separator: "\n"))
                    .font(.system(size: 12).monospaced())
                    .textSelection(.enabled)
            }
        }
        if !body.risks.isEmpty { Labeled(title: "Risks") { Text(body.risks) } }
        if !brief.unsupportedClaims.isEmpty {
            Labeled(title: "Not verified against the save") {
                ForEach(brief.unsupportedClaims, id: \.self) { Text("• \($0)") }
            }
            .help("Jev couldn't confirm these from the saved post or README. They may come from the linked page.")
        }
    }

    @ViewBuilder private func followUpBody(_ followUp: Brief.FollowUp) -> some View {
        Text(followUp.summary).foregroundStyle(Theme.ink)
        if let why = followUp.whyNow { Callout(lead: "Why now", text: why) }
        Labeled(title: "Next steps") {
            ForEach(Array(followUp.nextSteps.enumerated()), id: \.offset) { index, step in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("\(index + 1). \(step.text)")
                    if step.who == "beacon" {
                        Text("hand off")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(Theme.quiet)
                            .padding(.horizontal, 7).padding(.vertical, 2)
                            .background(Theme.control, in: .capsule)
                    }
                }
            }
        }
        ForEach(Array(followUp.drafts.enumerated()), id: \.offset) { index, draft in
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(draft.to.map { "\(draft.kind.capitalized) to \($0)" } ?? draft.kind.capitalized)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Theme.ink)
                    Spacer()
                    Button {
                        Platform.copy(edits[index] ?? draft.body)
                        withAnimation(.spring(duration: 0.3, bounce: 0.4)) { copied = index }
                    } label: {
                        Label(copied == index ? "Copied" : "Copy", systemImage: copied == index ? "checkmark" : "doc.on.doc")
                            .contentTransition(.symbolEffect(.replace))
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(Theme.ink)
                            .padding(.horizontal, 10).padding(.vertical, 5)
                            .background(Theme.tray, in: .capsule)
                    }
                    .buttonStyle(PressScale())
                }
                TextField("Draft", text: Binding(get: { edits[index] ?? draft.body }, set: { edits[index] = $0; copied = nil }),
                          axis: .vertical)
                    .textFieldStyle(.plain)
                    .foregroundStyle(Theme.ink)
            }
            .padding(14)
            .background(Theme.soft, in: .rect(cornerRadius: 18, style: .continuous))
        }
        if !followUp.sources.isEmpty {
            Text(followUp.sources.joined(separator: "\n"))
                .font(.system(size: 11))
                .foregroundStyle(Theme.quiet)
                .textSelection(.enabled)
        }
    }

    /// Secondary actions in a quiet row; Dismiss, Done and the main action as Triage's three big buttons.
    private var actions: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                if brief.brief != nil {
                    #if os(macOS)
                    // Opening a thread yourself stays possible; Beacon then tracks nothing.
                    small("Claude Code", symbol: Destination.claude.symbol) { store.send(brief, to: .claude) }
                    small("Codex", symbol: Destination.codex.symbol) { store.send(brief, to: .codex) }
                    small("Copy prompt", symbol: "doc.on.doc") { store.copyPrompt(brief) }
                    #else
                    small(brief.forMac == true ? "On Mac's list" : "Start on Mac", symbol: "desktopcomputer") { store.toggleForMac(brief) }
                    #endif
                }
                if let followUp = brief.followUp {
                    ShareLink(item: handOffText(brief, followUp, edits: edits)) {
                        Label("Hand off", systemImage: "square.and.arrow.up")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(Theme.ink)
                            .padding(.horizontal, 12).padding(.vertical, 6)
                            .background(Theme.soft, in: .capsule)
                    }
                    .buttonStyle(PressScale())
                }
                if let link = brief.link {
                    small("Open save", symbol: "arrow.up.right") { Platform.open(link) }
                }
            }
            HStack(spacing: 10) {
                // As in Triage, the agent's suggestion is the black button: Pass when it found nothing to do.
                let start = !brief.nothingToDo
                if start { big("Dismiss", primary: false) { finish(.dismissed) } }
                big("Done", primary: false) { finish(.done) }
                big(brief.startTitle, primary: start) { activity.start(itemId: brief.itemId, destination: brief.projectName) }
                    .help(brief.startHelp)
                if !start { big("Pass", primary: true) { finish(.dismissed) } }
            }
        }
    }

    private func small(_ title: String, symbol: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: symbol)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Theme.ink)
                .padding(.horizontal, 12).padding(.vertical, 6)
                .background(Theme.soft, in: .capsule)
        }
        .buttonStyle(PressScale())
    }

    private func big(_ title: String, primary: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) { bigLabel(title, primary: primary) }.buttonStyle(PressScale())
    }

    private func bigLabel(_ title: String, primary: Bool) -> some View {
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

/// The tray's sparkle box: the one line that matters most.
private struct Callout: View {
    let lead: String
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "sparkle")
                .font(.system(size: 13, weight: .bold))
                .foregroundStyle(Theme.accent)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 4) {
                Text(lead).font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.quiet)
                Text(text).foregroundStyle(Theme.ink)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.soft, in: .rect(cornerRadius: 18, style: .continuous))
    }
}

private struct Labeled<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.quiet)
            content.foregroundStyle(Theme.ink)
        }
    }
}

/// An agent at work: a turning sparkle, not a spinner.
private struct PlanningLine: View {
    let project: String
    let count: Int

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "sparkle")
                .font(.system(size: 13, weight: .bold))
                .foregroundStyle(Theme.accent)
                .symbolEffect(.rotate, options: .speed(0.4).repeating)
            Text("Planning \(project) · \(count) \(count == 1 ? "save" : "saves")")
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(Theme.ink)
        }
        .padding(.vertical, 10)
    }
}

/// The rare moment: nothing waiting on you.
private struct NothingPlanned: View {
    @State private var bounce = 0

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "sparkles")
                .font(.system(size: 52))
                .foregroundStyle(Theme.accent)
                .symbolEffect(.bounce, value: bounce)
            Text("Nothing to act on").font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.ink)
            Text("Do a project save or Keep an area save in Triage. Its plan lands here.")
                .multilineTextAlignment(.center)
                .foregroundStyle(Theme.quiet)
        }
        .onAppear { bounce += 1 }
    }
}

/// What the share sheet carries to Muse (WhatsApp), dots (ChatGPT) or Messages. You send it.
private func handOffText(_ brief: Brief, _ followUp: Brief.FollowUp, edits: [Int: String] = [:]) -> String {
    let steps = followUp.nextSteps.enumerated().map { "\($0.offset + 1). \($0.element.text)" }
    let drafts = followUp.drafts.indices.map { "Draft: " + (edits[$0] ?? followUp.drafts[$0].body) }
    return ([brief.title ?? "", followUp.summary, "Next steps:"] + steps + drafts + (brief.url.map { [$0] } ?? []))
        .filter { !$0.isEmpty }.joined(separator: "\n")
}

private extension Brief {
    /// A save's text can run several lines; its first line is the title.
    var headline: String { title?.split(separator: "\n").first.map(String.init) ?? "Saved item" }

    /// "Send to Travel" for an area's follow-up, "Build in Folio" for a project's plan.
    var startTitle: String { followUp == nil ? "Build in \(projectName)" : "Send to \(projectName)" }

    var startHelp: String {
        followUp == nil
            ? "Beacon hands the plan to \(projectName)'s agent in a fresh worktree; it never pushes. Activity tracks the run."
            : "Beacon hands this to \(projectName)'s agent, which writes drafts for you to look at. Activity tracks the run."
    }
}

// MARK: Hand-offs

/// One hand-off: the save, where it went and what it's waiting on, and its state as the pill.
private struct HandOffRow: View {
    let record: Activation
    let isSelected: Bool
    let open: () -> Void

    var body: some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(record.headline)
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                HStack(spacing: 4) {
                    ShelfLabel(project: Project(slug: record.destination, name: record.destinationLabel, fit: nil, kind: nil))
                        .layoutPriority(1)
                    if let line = record.line {
                        Text("· " + line).lineLimit(1)
                    }
                }
                .font(.system(size: 13))
                .foregroundStyle(Theme.quiet)
            }
            Spacer(minLength: 8)
            StatePill(state: record.state)
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
        .accessibilityHint("Shows the whole hand-off")
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

/// The state, in the row's pill spot: what you do next where you have something to do, a quiet word otherwise.
private struct StatePill: View {
    let state: Activation.State

    var body: some View {
        switch state {
        case .needsYou: Pill(title: "Answer", tint: Theme.accent)
        case .handedOff: Pill(title: "Send", tint: Theme.accent)
        case .readyToReview: Pill(title: "Review", tint: nil)
        case .running:
            Image(systemName: "sparkle")
                .font(.system(size: 13, weight: .bold))
                .foregroundStyle(Theme.accent)
                .symbolEffect(.rotate, options: .speed(0.4).repeating)
                .accessibilityLabel("Running")
        case .done:
            Image(systemName: "checkmark")
                .font(.system(size: 11, weight: .heavy))
                .foregroundStyle(Theme.quiet)
                .accessibilityLabel("Done")
        case .didntWork, .unknown:
            Image(systemName: "xmark")
                .font(.system(size: 11, weight: .heavy))
                .foregroundStyle(Theme.quiet)
                .accessibilityLabel("Didn't work")
        }
    }
}

/// Everything about one hand-off and what you can do with it: answer, send, review, close.
private struct HandOffTray: View {
    let record: Activation
    let activity: ActivityModel
    var close: (() -> Void)?
    @State private var reply = ""
    @State private var copied = false
    @FocusState private var replying: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 10) {
                ShelfLabel(project: Project(slug: record.destination, name: record.destinationLabel, fit: nil, kind: nil))
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                Text(record.stateTitle + " · " + record.runnerName)
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.quiet)
                    .lineLimit(1)
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

            ViewThatFits(in: .vertical) {
                content
                ScrollView { content }
            }
            // Top-aligned: a short hand-off sat in the middle of the space, under a blank band.
            .frame(maxHeight: TrayView.postHeight, alignment: .top)

            actions
        }
        .padding(20)
        .background(Theme.tray, in: .rect(cornerRadius: 34, style: .continuous))
        .shadow(color: .black.opacity(0.12), radius: 24, y: 12)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(record.headline)
                .font(.system(size: 20, weight: .bold))
                .kerning(-0.3)
                .foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            switch record.state {
            case .needsYou:
                Callout(lead: "\(record.runnerName) asks", text: record.needs ?? "It needs a decision from you.")
                if let summary = record.summary { Labeled(title: "What it did so far") { Text(summary) } }
                TextField("Your answer", text: $reply, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(2...6)
                    .focused($replying)
                    .foregroundStyle(Theme.ink)
                    .padding(14)
                    .background(Theme.soft, in: .rect(cornerRadius: 18, style: .continuous))
            case .handedOff:
                Callout(lead: "Send this to \(record.runnerName)",
                        text: "Beacon wrote the prompt. Hand it off, then mark it done once it's handled.")
                if let prompt = record.handoff {
                    Text(prompt)
                        .font(.system(size: 13))
                        .foregroundStyle(Theme.ink)
                        .textSelection(.enabled)
                        .lineLimit(14)
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Theme.soft, in: .rect(cornerRadius: 18, style: .continuous))
                }
            case .running:
                HStack(spacing: 10) {
                    Image(systemName: "sparkle")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundStyle(Theme.accent)
                        .symbolEffect(.rotate, options: .speed(0.4).repeating)
                    Text("\(record.runnerName) is working on it\(record.since.map { ", started \($0)" } ?? ""). "
                         + "Its state fills in when the run reports.")
                        .foregroundStyle(Theme.ink)
                }
            default:
                if let summary = record.summary { Text(summary).foregroundStyle(Theme.ink) }
            }
            if !record.links.isEmpty {
                Labeled(title: record.state == .readyToReview ? "What it made" : "Where it works") {
                    ForEach(record.links, id: \.self) { LinkLine(link: $0) }
                }
            }
        }
        .font(.system(size: 14))
        .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder private var actions: some View {
        let busy = activity.busy.contains(record.id)
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                if record.state == .handedOff, let prompt = record.handoff {
                    small(copied ? "Copied" : "Copy prompt", symbol: copied ? "checkmark" : "doc.on.doc") {
                        Platform.copy(prompt)
                        withAnimation(.spring(duration: 0.3, bounce: 0.4)) { copied = true }
                    }
                }
                if let link = record.link {
                    small("Open save", symbol: "arrow.up.right") { Platform.open(link) }
                }
            }
            switch record.state {
            case .needsYou:
                HStack(spacing: 10) {
                    big("Didn't work", primary: false) { closeAs(.didntWork) }
                    big("Send answer", primary: true) {
                        Task { if await activity.answer(record, reply) { reply = "" } }
                    }
                    .disabled(reply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .opacity(reply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.4 : 1)
                }
            case .handedOff:
                HStack(spacing: 10) {
                    big("Didn't work", primary: false) { closeAs(.didntWork) }
                    big("Done", primary: false) { closeAs(.done) }
                    if let prompt = record.handoff {
                        ShareLink(item: prompt) { bigLabel("Hand off", primary: true) }.buttonStyle(PressScale())
                    }
                }
            case .readyToReview:
                HStack(spacing: 10) {
                    big("Didn't work", primary: false) { closeAs(.didntWork) }
                    #if os(macOS)
                    if let folder = record.folder {
                        big("Open folder", primary: false) { _ = Platform.open(URL(fileURLWithPath: folder)) }
                    }
                    #endif
                    big("Done", primary: true) { closeAs(.done) }
                }
            default:
                EmptyView()
            }
        }
        .disabled(busy)
        .opacity(busy ? 0.6 : 1)
    }

    private func closeAs(_ state: Activation.State) {
        Task { await activity.close(record, as: state) }
    }

    private func small(_ title: String, symbol: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: symbol)
                .contentTransition(.symbolEffect(.replace))
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Theme.ink)
                .padding(.horizontal, 12).padding(.vertical, 6)
                .background(Theme.soft, in: .capsule)
        }
        .buttonStyle(PressScale())
    }

    private func big(_ title: String, primary: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) { bigLabel(title, primary: primary) }.buttonStyle(PressScale())
    }

    private func bigLabel(_ title: String, primary: Bool) -> some View {
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

/// One thing a run made or used. On the Mac a folder or file opens in Finder and a branch name copies;
/// the phone can't open the Mac's files, so it copies the path, and opens only web links.
private struct LinkLine: View {
    let link: Activation.Link

    var body: some View {
        Button(action: act) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: symbol).foregroundStyle(Theme.quiet).frame(width: 16)
                Text(shown)
                    .font(.system(size: 12).monospaced())
                    .foregroundStyle(Theme.ink)
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .multilineTextAlignment(.leading)
            }
        }
        .buttonStyle(.plain)
        .help(help)
    }

    private var symbol: String {
        switch link.kind {
        case "folder": "folder"
        case "branch": "arrow.triangle.branch"
        case "url": "link"
        default: "doc"
        }
    }

    private var shown: String {
        link.kind == "folder" || link.kind == "file" ? (link.value as NSString).abbreviatingWithTildeInPath : link.value
    }

    private var opensHere: Bool {
        #if os(macOS)
        link.kind != "branch"
        #else
        link.kind == "url"
        #endif
    }

    private var help: String { opensHere ? "Open" : "Copy" }

    private func act() {
        if opensHere {
            _ = Platform.open(link.kind == "url" ? URL(string: link.value) ?? URL(fileURLWithPath: "/") : URL(fileURLWithPath: link.value))
        } else {
            Platform.copy(link.value)
        }
    }
}

private extension Activation {
    /// The row's second line: the question, what's ready, or how far it got.
    var line: String? {
        switch state {
        case .needsYou: needs
        case .handedOff: "Ready to send to \(runnerName)"
        case .running: "\(runnerName) is working"
        default: summary
        }
    }

    var stateTitle: String {
        switch state {
        case .needsYou: "Needs you"
        case .handedOff: "Handed off"
        case .readyToReview: "Ready to review"
        case .running: "Running"
        case .done: "Done"
        case .didntWork, .unknown: "Didn't work"
        }
    }

    /// The worktree or drafts folder, for the Mac's Open folder.
    var folder: String? { links.first { $0.kind == "folder" }?.value }

    /// "10:42" today, "Nov 18" before.
    var since: String? {
        let parse = ISO8601DateFormatter()
        parse.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = parse.date(from: startedAt) ?? ISO8601DateFormatter().date(from: startedAt) else { return nil }
        return Calendar.current.isDateInToday(date) ? date.formatted(date: .omitted, time: .shortened)
            : date.formatted(.dateTime.month(.abbreviated).day())
    }
}
