import Foundation

struct Project: Codable, Hashable, Identifiable {
    let slug: String
    let name: String
    let fit: Double?
    /// "project" (a codebase with an agent) or "area" (a lifestyle shelf such as Food).
    let kind: String?

    var isArea: Bool { kind == "area" }

    var id: String { slug }
    var matchPercent: Int { Int(((fit ?? 0) * 100).rounded()) }
}

struct Preview: Codable, Hashable {
    struct Media: Codable, Hashable {
        let type: String?
        let url: String?
        let previewUrl: String?
        let width: Double?
        let height: Double?

        enum CodingKeys: String, CodingKey { case type, url, width, height, previewUrl = "preview_url" }

        var isVideo: Bool { type == "video" || type == "animated_gif" }
        var aspect: Double { guard let width, let height, height > 0 else { return 16 / 9 }; return width / height }
        var imageURL: URL? { (previewUrl ?? url).flatMap(URL.init(string:)) }
    }

    struct Quoted: Codable, Hashable {
        let author: String?
        let name: String?
        let text: String?
    }

    struct Repo: Codable, Hashable {
        let name: String?
        let language: String?
        let stars: Int?
        let topics: [String]?
    }

    let text: String?
    let authorName: String?
    let likes: Int?
    let media: [Media]?
    let quoted: Quoted?
    let repo: Repo?
    let link: String?
    let date: String?

    enum CodingKeys: String, CodingKey {
        case text, likes, media, quoted, repo, link, date
        case authorName = "author_name"
    }
}

struct Item: Codable, Identifiable, Hashable {
    static let pickCount = 5

    /// Shared with the widgets: one Do pick per destination before taking another from any.
    static func picks(from items: [Item]) -> [Item] {
        var seen = Set<String>(), first: [Item] = [], later: [Item] = []
        for item in items where item.suggested == .doIt {
            if seen.insert(item.bestProject?.slug ?? "").inserted { first.append(item) } else { later.append(item) }
        }
        return Array((first + later).prefix(pickCount))
    }

    let id: Int
    let source: String
    let title: String?
    let body: String?
    let url: String?
    let author: String?
    let createdAt: String?
    let intent: String?
    let projects: [Project]
    let preview: Preview?
    let stack: StackInfo?
    let gist: Gist?

    enum CodingKeys: String, CodingKey {
        case id, source, title, body, url, author, intent, projects, preview, stack, gist
        case createdAt = "created_at"
    }

    var text: String {
        var body = (preview?.text ?? body ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        // Vault and wiki notes start with YAML frontmatter; show the note itself.
        if body.hasPrefix("---"), let end = body.range(of: "\n---", range: body.index(body.startIndex, offsetBy: 3)..<body.endIndex) {
            body = String(body[end.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return body.isEmpty ? (title ?? "") : body
    }

    /// Notes from the vault, wiki and read-later sources are markdown; tweets and repo
    /// blurbs stay literal so a stray asterisk is not read as emphasis.
    var displayText: AttributedString {
        let text = text
        guard !["x", "github"].contains(source) else { return AttributedString(text) }
        let cleaned = text.replacingOccurrences(of: "[[", with: "").replacingOccurrences(of: "]]", with: "")
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { line in
                // Inline-only parsing leaves "## Heading" literal; show headings as bold lines.
                let heading = line.drop { $0 == "#" }
                return heading.count < line.count && heading.first == " "
                    ? "**\(heading.dropFirst())**" : String(line)
            }
            .joined(separator: "\n")
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: cleaned, options: options)) ?? AttributedString(text)
    }

    var isGitHub: Bool { source == "github" }

    var byline: String {
        if isGitHub { return title ?? "GitHub repository" }
        guard let author, !author.isEmpty else { return "Unknown" }
        return author.hasPrefix("@") ? author : "@\(author)"
    }

    /// Person or repo name shown in bold; the @handle sits beside it.
    var displayName: String {
        if isGitHub { return preview?.repo?.name ?? title ?? "GitHub repository" }
        return preview?.authorName ?? byline
    }

    var handle: String? { !isGitHub && preview?.authorName != nil ? byline : nil }

    var sourceName: String {
        switch source {
        case "x": "X"
        case "github": "GitHub"
        default: source.capitalized
        }
    }

    var sourceSymbol: String { isGitHub ? "star.fill" : "bookmark.fill" }

    var link: URL? { url.flatMap(URL.init(string:)) }

    var host: String? {
        guard let host = link?.host() else { return nil }
        return host.hasPrefix("www.") ? String(host.dropFirst(4)) : host
    }

    var date: Date? {
        guard let createdAt = preview?.date ?? createdAt else { return nil }
        let full = ISO8601DateFormatter()
        full.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return full.date(from: createdAt) ?? ISO8601DateFormatter().date(from: createdAt)
    }

    var bestProject: Project? { projects.first }

    /// Area saves are kept on a shelf; Do (plan it with an agent) only makes sense for a codebase.
    var isArea: Bool { bestProject?.isArea == true }

    /// The line to decide on: Sonnet's takeaway, else the post's first line.
    var headline: String {
        gist?.takeaway ?? text.split(separator: "\n").first.map(String.init) ?? text
    }

    /// Beacon's suggestion; near-duplicates and items without a gist default to Reject.
    var suggested: Decision {
        guard stack?.duplicateOf == nil, let gist else { return .reject }
        return gist.decision
    }
}

/// What a post actually says, written by Sonnet from the full text, plus a suggested decision.
struct Gist: Codable, Hashable {
    let takeaway: String
    let kind: String
    let suggestion: String
    let reason: String

    var decision: Decision {
        switch suggestion {
        case "do": .doIt
        case "read": .read
        default: .reject
        }
    }
}

struct Brief: Codable, Identifiable, Hashable {
    struct FollowUp: Codable, Hashable {
        struct Step: Codable, Hashable { let text: String; let who: String }
        struct Draft: Codable, Hashable { let kind: String; let to: String?; let body: String }
        let summary: String
        let whyNow: String?
        let nextSteps: [Step]
        let drafts: [Draft]
        let sources: [String]
        enum CodingKeys: String, CodingKey {
            case summary, drafts, sources
            case whyNow = "why_now", nextSteps = "next_steps"
        }
    }
    struct Body: Codable, Hashable {
        let verdict: String
        let summary: String
        let whyHere: String
        let nextStep: String
        let steps: [String]
        let files: [String]
        let effort: String
        let risks: String
        let taskPrompt: String
        let itemClaims: [String]?

        enum CodingKeys: String, CodingKey {
            case verdict, summary, steps, files, effort, risks
            case whyHere = "why_here", nextStep = "next_step", taskPrompt = "task_prompt", itemClaims = "item_claims"
        }
    }

    let itemId: Int
    let workstream: String
    let projectName: String
    let projectPath: String?
    let title: String?
    let url: String?
    let actionable: Double?
    let impact: Double?
    let brief: Body?
    let unsupportedClaims: [String]
    /// Flagged on the phone to start from the Mac, where Claude Code and Codex can open it.
    var forMac: Bool?
    var followUp: FollowUp? = nil

    enum CodingKeys: String, CodingKey {
        case workstream, title, url, actionable, impact, brief
        case itemId = "item_id", projectName = "project_name", projectPath = "project_path"
        case unsupportedClaims = "unsupported_claims", forMac = "for_mac"
        case followUp = "follow_up"
    }

    var id: String { "\(itemId)-\(workstream)" }
    var link: URL? { url.flatMap(URL.init(string:)) }
    /// The agent found nothing to do here (skip, or reference only).
    var nothingToDo: Bool { ["skip", "reference only"].contains(brief?.verdict ?? "") }
}

/// Where a brief's task goes. Both links pre-fill a new thread; nothing runs until you press Enter.
enum Destination: String {
    case claude = "Claude Code", codex = "Codex"

    var symbol: String { self == .claude ? "sparkle" : "chevron.left.forwardslash.chevron.right" }

    func url(for brief: Brief) -> URL? {
        guard let body = brief.brief else { return nil }
        var prompt = "\(body.taskPrompt)\n\nContext: Beacon brief for “\(brief.title ?? "saved item")”"
        if let url = brief.url { prompt += " (\(url))" }
        prompt += ". Next step: \(body.nextStep)"
        var components: URLComponents
        switch self {
        case .claude:
            components = URLComponents(string: "claude://code/new")!
            components.queryItems = [URLQueryItem(name: "q", value: String(prompt.prefix(3500)))]
            if let path = brief.projectPath { components.queryItems?.append(URLQueryItem(name: "folder", value: path)) }
        case .codex:
            // Codex links take no folder, so name it in the prompt.
            if let path = brief.projectPath { prompt = "Work in the project at \(path).\n\n" + prompt }
            components = URLComponents(string: "codex://threads/new")!
            components.queryItems = [URLQueryItem(name: "prompt", value: String(prompt.prefix(3500)))]
        }
        return components.url
    }
}

/// How to undo an Apply: the Mac has ⌘Z, the iPhone has the Undo button over the stack list.
#if os(macOS)
let undoHint = "⌘Z"
#else
let undoHint = "The Undo button"
#endif

/// What a swipe or key press records. Raw values are Beacon lifecycle statuses.
enum Decision: String, CaseIterable {
    case reject = "rejected"
    /// Worth knowing: goes to Readwise Reader, no agent.
    case read = "read"
    /// Worth acting on: the project's agent plans it.
    case doIt = "reviewed"

    var title: String {
        switch self {
        case .reject: "Reject"
        case .read: "Read"
        case .doIt: "Do"
        }
    }

    var symbol: String {
        switch self {
        case .reject: "xmark"
        case .read: "eyeglasses"
        case .doIt: "bolt.fill"
        }
    }

    var hint: String {
        switch self {
        case .reject: "X"
        case .read: "R"
        case .doIt: "D"
        }
    }

    /// "Keep" instead of "Do" on an area save.
    func title(for item: Item) -> String { self == .doIt && item.isArea ? "Keep" : title }

    /// What happens to an item when its stack is applied with this choice.
    func consequence(for item: Item) -> String {
        let project = item.bestProject?.name ?? "the project"
        if self == .doIt, item.isArea { return "On Apply, it's kept on your \(project) shelf. No agent runs." }
        switch self {
        case .doIt: return "On Apply, the \(project) agent plans this with your other Do picks. The plan is in Briefs in about 2 minutes; your code isn't touched until you send it."
        case .read: return "Saved to Readwise Reader, tagged \(item.bestProject?.slug ?? "beacon"). No agent runs."
        case .reject: return "Leaves the queue. \(undoHint) brings it back."
        }
    }
}

/// Where Jev put an item: a theme shared with similar queued items.
struct StackInfo: Codable, Hashable {
    let theme: String
    let description: String
    let rank: Int?
    let duplicateOf: Int?

    enum CodingKeys: String, CodingKey { case theme, description, rank, duplicateOf = "duplicate_of" }
}

/// A themed group of queued items, each row prefilled with Beacon's suggestion.
struct Stack: Identifiable, Hashable {
    let theme: String
    let description: String
    /// Suggested Do first, then Read, then Reject, so the ones worth a look lead.
    let items: [Item]

    init(theme: String, description: String, items: [Item]) {
        self.theme = theme
        self.description = description
        let order: [Decision] = [.doIt, .read, .reject]
        self.items = items.enumerated().sorted {
            let (a, b) = (order.firstIndex(of: $0.element.suggested)!, order.firstIndex(of: $1.element.suggested)!)
            return a == b ? $0.offset < $1.offset : a < b
        }.map(\.element)
    }

    /// An area's stack (every item on the same shelf): Do reads as Keep.
    var isArea: Bool { !items.isEmpty && items.allSatisfy(\.isArea) }

    /// The theme itself, so list selection reads the same on the Mac (explicit tag) and iPhone (row id).
    var id: String { theme }
}

/// One agent run's output for a project: what to start with, overlaps, and a brief per item.
struct Plan: Codable, Identifiable, Hashable {
    let workstream: String
    let projectName: String
    let projectPath: String?
    let summary: String
    let overlaps: [String]
    var briefs: [Brief]

    enum CodingKeys: String, CodingKey {
        case workstream, summary, overlaps, briefs
        case projectName = "project_name", projectPath = "project_path"
    }

    var id: String { workstream }
}

/// How to narrow the library: the one search box (words and meaning fused, then follow-up steps),
/// or pictures like one save. `.words` and `.meaning` stay for older callers; the Library no longer offers them.
enum LibrarySearch: Equatable {
    case search(String, refine: [String])
    case words(String)
    case meaning(String)
    case similar(Int)

    /// `beacon library` arguments.
    var arguments: [String] {
        switch self {
        // "--flag=value" so text that starts with a dash stays text.
        case let .search(text, refine): ["--search=\(text)"] + refine.map { "--refine=\($0)" }
        case .words(let text): ["--query=\(text)"]
        case .meaning(let text): ["--meaning=\(text)"]
        case .similar(let id): ["--similar", String(id)]
        }
    }

    /// `beacon serve` query items.
    var queryItems: [URLQueryItem] {
        switch self {
        case let .search(text, refine): [URLQueryItem(name: "search", value: text)] + refine.map { URLQueryItem(name: "refine", value: $0) }
        case .words(let text): [URLQueryItem(name: "q", value: text)]
        case .meaning(let text): [URLQueryItem(name: "meaning", value: text)]
        case .similar(let id): [URLQueryItem(name: "similar", value: String(id))]
        }
    }
}

/// One page of `beacon library`: saves with pictures plus GitHub stars, newest first.
struct LibraryPage: Codable {
    struct Entry: Codable, Identifiable, Hashable {
        struct Picture: Codable, Hashable { let url: String; let width: Double?; let height: Double? }
        struct Repo: Codable, Hashable { let name: String?; let language: String? }

        let id: Int
        let source: String
        let url: String?
        let title: String?
        let text: String?
        let author: String?
        let authorName: String?
        let repo: Repo?
        let media: [Picture]
        /// The area (shelf) slug Jev placed it on, if any.
        let area: String?
        /// Search results only: which rankings found it ("words", "pictures", "meaning").
        var foundBy: [String]? = nil
        /// Search results only: "strong", or "maybe" for the "Might also fit" section.
        var fit: String? = nil

        enum CodingKeys: String, CodingKey {
            case id, source, url, title, text, author, repo, media, area, fit
            case authorName = "author_name", foundBy = "found_by"
        }

        /// Pictures you added live on the Mac ("/media/…"), served by `beacon serve`.
        var picture: URL? { media.first.flatMap { URL(string: $0.url, relativeTo: LibraryPage.mediaBase)?.absoluteURL } }
        var link: URL? { url.flatMap(URL.init(string:)) }
        var caption: String { repo?.name ?? text?.split(separator: "\n").first.map(String.init) ?? title ?? "" }
    }

    let total: Int
    let nextOffset: Int?
    let items: [Entry]
    /// Search only: what answered ("words", "pictures", "meaning"). Without pictures or meaning,
    /// the meaning search isn't running on this Mac.
    var searched: [String]? = nil
    /// Search with follow-up steps: how many saves the steps set aside.
    var setAside: Int? = nil

    /// Where "/media/…" pictures load from: `beacon serve` on this Mac, or over Tailscale on the phone.
    nonisolated(unsafe) static var mediaBase = URL(string: "http://127.0.0.1:8765")!

    enum CodingKeys: String, CodingKey { case total, items, searched, nextOffset = "next_offset", setAside = "set_aside" }
}

/// Something Beacon's autopilot did on its own: passed a save, or sent it to Readwise Reader.
struct HandledItem: Codable, Hashable, Identifiable {
    /// The save's item id.
    let id: Int
    /// "pass" or "read".
    let action: String
    let reason: String?
    let title: String?
    let url: String?

    var verb: String { action == "read" ? "Sent to Reader" : "Passed" }
}

/// `beacon references`: which saves fit a brief, best first, with saves that repeat a picture last.
struct ReferenceReview: Codable, Hashable {
    struct Save: Codable, Hashable, Identifiable {
        let id: Int
        /// "strong", "maybe" or "off".
        let fit: String
        /// The better-fitting save with the same picture, if this one repeats it.
        let duplicateOf: Int?
        /// Why, in a line: where its picture ranks for the brief, and what Jev made of its words.
        let reason: String

        enum CodingKeys: String, CodingKey { case id, fit, reason, duplicateOf = "duplicate_of" }
    }

    let wordsChecked: Bool
    /// Set when Jev couldn't be reached and the review fell back to pictures.
    let note: String?
    let saves: [Save]

    enum CodingKeys: String, CodingKey { case note, saves, wordsChecked = "words_checked" }
}
