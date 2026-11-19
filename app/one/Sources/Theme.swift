import SwiftUI

/// Beach glass on pale yellow: a warm page, white trays, black ink, one blue accent.
enum Theme {
    static let page = Color(hex: 0xFFFAE6)
    /// Behind a tray on the Mac, and the resting fill of secondary buttons.
    static let deep = Color(hex: 0xFBF1CC)
    static let soft = Color(hex: 0xFBF5DF)
    static let control = Color(hex: 0xF6ECC6)
    static let tray = Color.white
    static let ink = Color(hex: 0x111114)
    static let quiet = Color(hex: 0x6B6F76)
    static let accent = Color(hex: 0x2F5BFF)

    /// Library's page: white, so it never reads as Triage's yellow.
    static let shelf = Color.white
    static let shelfSide = Color(hex: 0xF7F6F2)

    /// The areas in Beacon's config.json, in shelf order.
    // shortcut: a copy of config.json's "areas"; a new area there needs a line here too, or Library won't show its shelf.
    static let areas: [(slug: String, name: String)] = [
        ("style", "Style"), ("home", "Home"), ("food", "Food"), ("travel", "Travel"),
        ("reading", "Reading"), ("ideas", "Ideas"), ("activities", "Activities & Dates"), ("websites", "Websites"),
    ]

    static func color(slug: String?) -> Color? {
        color(for: slug.map { Project(slug: $0, name: "", fit: nil, kind: "area") })
    }

    /// One color per lifestyle area; projects (codebases) have none and show a square instead.
    static func color(for project: Project?) -> Color? {
        switch project?.slug {
        case "style": Color(hex: 0xE66A92)
        case "home": Color(hex: 0xF0A43A)
        case "food": Color(hex: 0xF07A5A)
        case "travel": Color(hex: 0x3FB8A0)
        case "reading": Color(hex: 0x5B7186)
        case "ideas": Color(hex: 0x8C7CF0)
        case "activities": Color(hex: 0x8FA34A)
        case "websites": accent
        default: nil
        }
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(red: Double(hex >> 16 & 0xFF) / 255, green: Double(hex >> 8 & 0xFF) / 255, blue: Double(hex & 0xFF) / 255)
    }
}

/// "TODAY'S PICKS · 5": the small capitals over each group, with the area's dot when it has one.
struct SectionTitle: View {
    let title: String
    let count: Int
    var slug: String? = nil

    var body: some View {
        HStack(spacing: 6) {
            if let color = Theme.color(slug: slug) { Circle().fill(color).frame(width: 7, height: 7) }
            Text(title.uppercased())
            Text("· \(count)").contentTransition(.numericText())
        }
        .font(.system(size: 12, weight: .semibold))
        .kerning(0.8)
        .foregroundStyle(Theme.quiet)
        .padding(.top, 26)
        .padding(.bottom, 4)
        .transition(.opacity)
    }
}

/// "● Style" for an area, "▢ Agent Workflows" for a project.
struct ShelfLabel: View {
    let project: Project?

    var body: some View {
        HStack(spacing: 6) {
            if let color = Theme.color(for: project) {
                Circle().fill(color).frame(width: 7, height: 7)
            } else {
                RoundedRectangle(cornerRadius: 2.5).strokeBorder(Theme.quiet, lineWidth: 1.4).frame(width: 9, height: 9)
            }
            Text(project?.name ?? "Unsorted")
        }
    }
}
