import SwiftUI

struct BriefsView: View {
    @Bindable var store: TriageStore

    var body: some View {
        let plans = store.plans
        let planning = store.planningRows
        Group {
            if plans.isEmpty && planning.isEmpty {
                ContentUnavailableView {
                    Label("No Plans Yet", systemImage: "sparkles.rectangle.stack")
                } description: {
                    Text("Press Do (→) on cards in Triage. About 20 seconds after your last Do for a project, its agent reads the repo once and plans everything you marked.")
                }
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 18) {
                        ForEach(planning, id: \.project) { PlanningRow(project: $0.project, count: $0.items.count) }
                        let flagged = plans.flatMap { plan in plan.briefs.enumerated().filter { $0.element.forMac == true } }
                        if !flagged.isEmpty {
                            Text("Start on the Mac").font(.title2.weight(.bold)).padding(.top, 10)
                            ForEach(flagged, id: \.element.id) { BriefCard(brief: $0.element, rank: $0.offset + 1, store: store) }
                        }
                        ForEach(plans) { plan in
                            PlanHeader(plan: plan)
                            ForEach(Array(plan.briefs.enumerated()).filter { $0.element.forMac != true }, id: \.element.id) { index, brief in
                                BriefCard(brief: brief, rank: index + 1, store: store)
                            }
                        }
                    }
                    .frame(maxWidth: 720)
                    .padding(.horizontal, TriagePanes.highlightsSelection ? 32 : 16)
                    .padding(.vertical, 28)
                    .frame(maxWidth: .infinity)
                }
            }
        }
        .animation(.spring(duration: 0.35), value: plans)
    }
}

struct PlanningRow: View {
    let project: String
    let count: Int

    var body: some View {
        HStack(spacing: 14) {
            ProgressView().controlSize(.small)
            Text("Planning \(project) · \(count) \(count == 1 ? "item" : "items")…")
                .font(.subheadline.weight(.medium))
            Spacer()
        }
        .padding(18)
        .background(.background.opacity(0.6), in: .rect(cornerRadius: 18, style: .continuous))
        .overlay { RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(.separator.opacity(0.5), style: StrokeStyle(lineWidth: 1, dash: [4, 4])) }
    }
}

struct PlanHeader: View {
    let plan: Plan

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(plan.projectName).font(.title2.weight(.bold))
                Text("\(plan.briefs.count) to act on")
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(.quaternary.opacity(0.6), in: .capsule)
            }
            if !plan.summary.isEmpty {
                Text(plan.summary).foregroundStyle(.secondary)
            }
            ForEach(plan.overlaps, id: \.self) { overlap in
                Label(overlap, systemImage: "arrow.triangle.merge")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.top, 10)
    }
}

struct BriefCard: View {
    let brief: Brief
    let rank: Int
    let store: TriageStore
    @State private var expanded = false

    var body: some View {
        if let followUp = brief.followUp {
            VStack(alignment: .leading, spacing: 14) {
                Text(brief.title ?? "Follow-up").font(.headline)
                Text(followUp.summary)
                if let why = followUp.whyNow { Text(why).foregroundStyle(.secondary) }
                ForEach(Array(followUp.nextSteps.enumerated()), id: \.offset) { index, step in
                    Text("\(index + 1). \(step.text) (\(step.who == "beacon" ? "hand off" : "you"))")
                }
                ForEach(Array(followUp.drafts.enumerated()), id: \.offset) { _, draft in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(draft.to.map { "\(draft.kind.capitalized) to \($0)" } ?? draft.kind.capitalized).font(.subheadline.bold())
                        Text(draft.body).textSelection(.enabled)
                        Button("Copy") { Platform.copy(draft.body) }
                    }
                }
                if !followUp.sources.isEmpty {
                    Text(followUp.sources.joined(separator: "\n")).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
                HStack {
                    // The share sheet reaches Muse (WhatsApp), dots (ChatGPT) or Messages; you send it yourself.
                    ShareLink("Hand off", item: handOff(followUp))
                    Button("Dismiss") { store.dismiss(brief) }
                }
            }
            .padding(22)
            .background(.background, in: .rect(cornerRadius: 20, style: .continuous))
        } else if let body = brief.brief {
            VStack(alignment: .leading, spacing: 14) {
                header(body)
                Text(brief.title ?? "")
                    .font(.headline)
                    .lineLimit(2)
                Text(body.summary)
                    .foregroundStyle(.secondary)
                nextStep(body)
                if expanded { details(body) }
                actions
            }
            .padding(22)
            .background(.background, in: .rect(cornerRadius: 20, style: .continuous))
            .overlay { RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(.separator.opacity(0.6), lineWidth: 0.5) }
            .shadow(color: .black.opacity(0.06), radius: 14, y: 6)
        }
    }

    private func handOff(_ followUp: Brief.FollowUp) -> String {
        let steps = followUp.nextSteps.enumerated().map { "\($0.offset + 1). \($0.element.text)" }
        return ([brief.title ?? "", followUp.summary, "Next steps:"] + steps + (brief.url.map { [$0] } ?? []))
            .filter { !$0.isEmpty }.joined(separator: "\n")
    }

    private func header(_ body: Brief.Body) -> some View {
        HStack(spacing: 8) {
            Text(brief.projectName)
                .font(.caption.weight(.semibold))
                .lineLimit(1)
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(.quaternary.opacity(0.6), in: .capsule)
            VerdictBadge(verdict: body.verdict)
            Spacer()
            Label(body.effort, systemImage: "clock")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("#\(rank)")
                .font(.caption.weight(.bold).monospacedDigit())
                .foregroundStyle(.secondary)
                .help("The project agent's order: start with #1")
        }
    }

    private func nextStep(_ body: Brief.Body) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("NEXT STEP")
                .font(.caption2.weight(.bold))
                .foregroundStyle(.tint)
            Text(body.nextStep)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color.accentColor.opacity(0.08), in: .rect(cornerRadius: 12, style: .continuous))
    }

    private func details(_ body: Brief.Body) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            section("Why here") { Text(body.whyHere) }
            if !body.steps.isEmpty {
                section("Steps") {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(body.steps.enumerated()), id: \.offset) { index, step in
                            Text("\(index + 1). \(step)")
                        }
                    }
                }
            }
            if !body.files.isEmpty {
                section("Files") {
                    Text(body.files.map { ($0 as NSString).abbreviatingWithTildeInPath }.joined(separator: "\n"))
                        .font(.caption.monospaced())
                        .textSelection(.enabled)
                }
            }
            section("Risks") { Text(body.risks) }
            if !brief.unsupportedClaims.isEmpty {
                Label {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Not verified against the saved item").font(.caption.weight(.semibold))
                        ForEach(brief.unsupportedClaims, id: \.self) { Text("• \($0)").font(.caption) }
                    }
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
                .help("Jev could not confirm these from the saved post or README. They may come from the linked page.")
            }
        }
        .foregroundStyle(.primary)
        .transition(.opacity.combined(with: .move(edge: .top)))
    }

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title.uppercased())
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)
            content()
        }
    }

    /// One row on the Mac; on a phone the buttons wrap onto as many rows as they need.
    private var actions: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 10) { send; more; Spacer(); close }
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) { send }
                HStack(spacing: 10) { more; Spacer(); close }
            }
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) { send }
                HStack(spacing: 10) { more }
                HStack(spacing: 10) { close }
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.regular)
        .lineLimit(1)
    }

    @ViewBuilder private var send: some View {
        #if os(iOS)
        // Claude Code and Codex links only open on the Mac, so the phone queues the brief there instead.
        Button { store.toggleForMac(brief) } label: {
            brief.forMac == true ? Label("On Mac's List", systemImage: "checkmark.circle.fill")
                : Label("Start on Mac", systemImage: "laptopcomputer")
        }
        .buttonStyle(.borderedProminent)
        .tint(brief.forMac == true ? .green : .accentColor)
        Button { store.copyPrompt(brief) } label: { Image(systemName: "doc.on.doc") }
        #else
        Button { store.send(brief, to: .claude) } label: { Label("Claude Code", systemImage: Destination.claude.symbol) }
            .buttonStyle(.borderedProminent)
            .help("Open a new Claude Code session in this project with the task typed in; press Enter there to start")
        Button { store.send(brief, to: .codex) } label: { Label("Codex", systemImage: Destination.codex.symbol) }
            .help("Open a new Codex thread with the task typed in; press Enter there to start")
        Button { store.copyPrompt(brief) } label: { Image(systemName: "doc.on.doc") }
            .help("Copy the task prompt to paste anywhere else")
        #endif
    }

    @ViewBuilder private var more: some View {
        Button { withAnimation(.spring(duration: 0.3)) { expanded.toggle() } } label: {
            Label(expanded ? "Less" : "Details", systemImage: expanded ? "chevron.up" : "chevron.down")
        }
        if let link = brief.link {
            Button { Platform.open(link) } label: { Label("Open Item", systemImage: "arrow.up.right.square") }
        }
    }

    @ViewBuilder private var close: some View {
        Button("Dismiss") { store.dismiss(brief) }
            .help("Not worth doing; hide this brief")
        Button { store.complete(brief) } label: { Label("Done", systemImage: "checkmark") }
            .help("You acted on it; the item is marked as used")
    }
}

struct VerdictBadge: View {
    let verdict: String

    var body: some View {
        Text(verdict.capitalized)
            .font(.caption.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(color.opacity(0.12), in: .capsule)
    }

    private var color: Color {
        switch verdict {
        case "do it": .green
        case "try it": .blue
        case "skip": .red
        default: .secondary
        }
    }
}
