import AppKit
import SwiftUI

@main
struct RenderWidgets {
    @MainActor
    static func main() throws {
        let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let sample = ImageRenderer(content: ReadingCorner().frame(width: 720, height: 720))
        guard let image = sample.cgImage, let imageData = WidgetSnapshot.jpeg(image) else { throw CocoaError(.fileWriteUnknown) }
        let cards = [
            WidgetCard(id: 1042, headline: "A reading corner in warm oak and linen", area: "home", jpeg: imageData),
            WidgetCard(id: 1043, headline: "Designing tools that leave room to think", area: "ideas"),
            WidgetCard(id: 1044, headline: "A long weekend in Copenhagen", area: "travel"),
        ]
        let snapshot = WidgetSnapshot(updated: Date(), count: 5, picks: cards, pictures: [cards[0]])

        // The executable checks the shared persistence and routing logic before making review artifacts.
        let file = output.appendingPathComponent("test-snapshot.json")
        try snapshot.write(to: file)
        let restored = WidgetSnapshot.read(from: file)
        precondition(restored.count == 5 && restored.picks.map(\.id) == cards.map(\.id))
        precondition(restored.picture(at: Date())?.jpeg == imageData)
        precondition(WidgetSnapshot.read(from: nil).picture(at: Date()) == nil)
        var rotating = snapshot
        rotating.pictures = cards
        precondition(rotating.picture(at: Date(timeIntervalSince1970: 0))?.id == 1042)
        precondition(rotating.picture(at: Date(timeIntervalSince1970: 10800))?.id == 1043)
        precondition(rotating.picture(at: Date(timeIntervalSince1970: 32400))?.id == 1042)
        precondition(BeaconLink(URL(string: "beacon://triage")!) == .triage)
        precondition(BeaconLink(URL(string: "beacon://save/1042")!) == .save(1042))
        for invalid in ["https://save/1042", "beacon://save/0", "beacon://save/no", "beacon://save/1/2", "beacon://unknown", "beacon://triage?x=1"] {
            precondition(BeaconLink(URL(string: invalid)!) == nil)
        }
        try FileManager.default.removeItem(at: file)

        // The same views ship on both platforms; sizes show the native Mac and iPhone proportions.
        for (platform, small, medium, large) in [
            ("iphone", CGSize(width: 170, height: 170), CGSize(width: 364, height: 170), CGSize(width: 364, height: 382)),
            ("mac", CGSize(width: 170, height: 170), CGSize(width: 360, height: 170), CGSize(width: 360, height: 360)),
        ] {
            try render(PicksWidgetView(snapshot: snapshot, small: true), size: small, to: output.appendingPathComponent("\(platform)-picks-small.png"))
            try render(PicksWidgetView(snapshot: snapshot, small: false), size: medium, to: output.appendingPathComponent("\(platform)-picks-medium.png"))
            try render(LibraryWidgetView(card: cards[0]), size: medium, to: output.appendingPathComponent("\(platform)-library-medium.png"))
            try render(LibraryWidgetView(card: cards[0]), size: large, to: output.appendingPathComponent("\(platform)-library-large.png"))
        }
        try render(PicksWidgetView(snapshot: WidgetSnapshot(), small: true), size: CGSize(width: 170, height: 170), to: output.appendingPathComponent("empty-picks-small.png"))
        try render(LibraryWidgetView(card: nil), size: CGSize(width: 364, height: 170), to: output.appendingPathComponent("empty-library-medium.png"))
        print("Snapshot, rotation and deep-link checks passed; 10 PNGs rendered.")
    }

    @MainActor
    static func render<V: View>(_ view: V, size: CGSize, to file: URL) throws {
        let renderer = ImageRenderer(content: view.frame(width: size.width, height: size.height)
            .clipShape(RoundedRectangle(cornerRadius: 22)).environment(\.colorScheme, .light))
        renderer.scale = 2
        guard let image = renderer.cgImage,
              let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else {
            throw CocoaError(.fileWriteUnknown)
        }
        try png.write(to: file, options: .atomic)
    }
}

/// A local sample save: an interior mood-board illustration, without a network dependency.
private struct ReadingCorner: View {
    var body: some View {
        GeometryReader { g in
            ZStack {
                Color(hex: 0xE9E0CF)
                Rectangle().fill(Color(hex: 0xC5AE89)).frame(height: 210).frame(maxHeight: .infinity, alignment: .bottom)
                Rectangle().fill(Color(hex: 0xEFF3E7)).frame(width: 210, height: 350).position(x: 550, y: 205)
                    .overlay {
                        Path { p in p.move(to: CGPoint(x: 550, y: 30)); p.addLine(to: CGPoint(x: 550, y: 380)); p.move(to: CGPoint(x: 445, y: 205)); p.addLine(to: CGPoint(x: 655, y: 205)) }
                            .stroke(Color(hex: 0xBAA78A), lineWidth: 12)
                    }
                Ellipse().fill(.black.opacity(0.12)).frame(width: 370, height: 75).position(x: 320, y: 610).blur(radius: 18)
                RoundedRectangle(cornerRadius: 36).fill(Color(hex: 0x9A6343)).frame(width: 270, height: 270).rotationEffect(.degrees(-6)).position(x: 260, y: 380)
                RoundedRectangle(cornerRadius: 22).fill(Color(hex: 0xBF8F64)).frame(width: 310, height: 100).position(x: 280, y: 520)
                RoundedRectangle(cornerRadius: 18).fill(Color(hex: 0xE5D9B9)).frame(width: 120, height: 140).rotationEffect(.degrees(12)).position(x: 320, y: 405)
                ForEach([180.0, 360.0], id: \.self) { x in
                    RoundedRectangle(cornerRadius: 5).fill(Color(hex: 0x6F4A30)).frame(width: 20, height: 72).position(x: x, y: 595)
                }
                Rectangle().fill(Color(hex: 0x76684C)).frame(width: 8, height: 360).position(x: 95, y: 350)
                Ellipse().fill(Color(hex: 0xF5EDDC)).frame(width: 150, height: 65).position(x: 95, y: 170)
                Ellipse().fill(Color(hex: 0x76684C)).frame(width: 80, height: 12).position(x: 95, y: 535)
                RoundedRectangle(cornerRadius: 8).fill(Color(hex: 0x80664A)).frame(width: 150, height: 18).position(x: 530, y: 480)
                Rectangle().fill(Color(hex: 0x80664A)).frame(width: 14, height: 105).position(x: 530, y: 540)
                RoundedRectangle(cornerRadius: 3).fill(Color(hex: 0x6B7D6A)).frame(width: 85, height: 13).rotationEffect(.degrees(-5)).position(x: 525, y: 463)
                RoundedRectangle(cornerRadius: 3).fill(Color(hex: 0xE5C591)).frame(width: 75, height: 10).rotationEffect(.degrees(4)).position(x: 525, y: 451)
            }
        }
    }
}
