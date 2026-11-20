import SwiftUI
#if os(macOS)
import AppKit
typealias PlatformImage = NSImage
#else
import UIKit
typealias PlatformImage = UIImage
#endif

/// Where Beacon lives: the Mac app runs `beacon.py` directly, the phone calls `beacon serve`.
protocol Backend: ActivityBackend, PickBackend {
    func queue() async throws -> [Item]
    func plans() async throws -> [Plan]
    func mark(_ status: String, ids: [Int]) async throws
    /// Saves to Readwise Reader, then marks the item adopted with a "read" note.
    func read(id: Int) async throws
    /// One read-only agent run over all of a project's Do items; roughly 30–90 seconds.
    func plan(project: String) async throws
    func setBriefStatus(_ brief: Brief, to status: String) async throws
    /// Marks the brief dispatched and its item `itemStatus` ("adopted" or "tried") in one transaction.
    func finishBrief(_ brief: Brief, itemStatus: String) async throws
    func setForMac(_ brief: Brief, _ on: Bool) async throws
    /// Runs the weekly pipeline now: fetch bookmarks and stars, score, stack, gist, plan. A few minutes.
    func sync() async throws
    /// A page of the visual library feed (saves with pictures, plus GitHub stars), newest first,
    /// or best first when searching.
    func library(offset: Int, limit: Int, area: String?, search: LibrarySearch?) async throws -> LibraryPage
    /// Keeps a web page or X post in the library (skips Triage).
    func keep(url: String) async throws
    /// Keeps a picture (its original file bytes) in the library.
    func keep(picture: Data, width: Int, height: Int, note: String) async throws
    /// Which of these saves fit what you're making, best first, repeats last. `words` lets Jev read
    /// their text (sent to TypeSafe); pictures are always compared on the Mac.
    func references(brief: String, ids: [Int], words: Bool) async throws -> ReferenceReview
    /// What autopilot did in the last two weeks and you haven't undone, newest first.
    func handled() async throws -> [HandledItem]
    /// Undoes autopilot for one save: it goes back to the queue.
    func unhandle(id: Int) async throws
}

/// ".jpg", ".png" and so on from a picture's first bytes; nil if Beacon doesn't keep that kind.
func pictureSuffix(_ data: Data) -> String? {
    let bytes = [UInt8](data.prefix(12))
    guard bytes.count >= 12 else { return nil }
    if bytes.starts(with: [0x89, 0x50, 0x4E, 0x47]) { return ".png" }
    if bytes.starts(with: [0xFF, 0xD8]) { return ".jpg" }
    if bytes.starts(with: Array("GIF8".utf8)) { return ".gif" }
    if bytes.starts(with: Array("RIFF".utf8)), Array(bytes[8..<12]) == Array("WEBP".utf8) { return ".webp" }
    if ["ftypheic", "ftypheix", "ftypmif1"].contains(String(decoding: bytes[4..<12], as: UTF8.self)) { return ".heic" }
    return nil
}

/// The few things the Mac and iPhone do differently.
@MainActor
enum Platform {
    @discardableResult
    static func open(_ url: URL) -> Bool {
        #if os(macOS)
        return NSWorkspace.shared.open(url)
        #else
        UIApplication.shared.open(url)
        return true
        #endif
    }

    static func tap() {
        #if os(macOS)
        NSHapticFeedbackManager.defaultPerformer.perform(.levelChange, performanceTime: .now)
        #else
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
        #endif
    }

    static func copy(_ text: String) {
        #if os(macOS)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        #else
        UIPasteboard.general.string = text
        #endif
    }
}

extension Image {
    init(platformImage: PlatformImage) {
        #if os(macOS)
        self.init(nsImage: platformImage)
        #else
        self.init(uiImage: platformImage)
        #endif
    }
}
