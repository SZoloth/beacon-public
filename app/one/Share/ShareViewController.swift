import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// The share sheet's entry point: finds the link being shared, then shows `ShareView`.
final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        let items = extensionContext?.inputItems as? [NSExtensionItem] ?? []
        let text = items.compactMap { $0.attributedContentText?.string }.joined(separator: " ")
        Task { @MainActor in
            let url = await Self.link(in: items, text: text)
            let view = ShareView(url: url, sharedText: text, done: { [weak self] in
                self?.extensionContext?.completeRequest(returningItems: nil)
            })
            let host = UIHostingController(rootView: view)
            addChild(host)
            host.view.frame = self.view.bounds
            host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            self.view.addSubview(host.view)
            host.didMove(toParent: self)
        }
    }

    /// The first web link among the shared items: a URL attachment, else a link inside shared text.
    static func link(in items: [NSExtensionItem], text: String) async -> URL? {
        for provider in items.flatMap({ $0.attachments ?? [] }) {
            if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier),
               let url = try? await provider.loadItem(forTypeIdentifier: UTType.url.identifier) as? URL,
               url.scheme?.hasPrefix("http") == true {
                return url
            }
            if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier),
               let shared = try? await provider.loadItem(forTypeIdentifier: UTType.plainText.identifier) as? String,
               let url = firstLink(in: shared) {
                return url
            }
        }
        return firstLink(in: text)
    }

    static func firstLink(in text: String) -> URL? {
        let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)
        let range = NSRange(text.startIndex..., in: text)
        return detector?.matches(in: text, range: range).compactMap(\.url).first { $0.scheme?.hasPrefix("http") == true }
    }
}
