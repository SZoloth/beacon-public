import LinkPresentation
import SwiftUI

/// The saved item as it appeared at its source: author, text, media, quoted post, link.
struct PostView: View {
    let item: Item

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                Avatar(seed: item.byline)
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.displayName).font(.headline).lineLimit(1)
                    HStack(spacing: 4) {
                        if let handle = item.handle { Text(handle) }
                        if let date = item.date {
                            Text("·")
                            Text(date, format: .relative(presentation: .named))
                        }
                    }
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                }
                Spacer()
                Image(systemName: item.sourceSymbol).foregroundStyle(.secondary).help(item.sourceName)
            }
            if !item.text.isEmpty {
                Text(item.displayText)
                    .font(.system(size: 15))
                    .lineSpacing(4)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let repo = item.preview?.repo { RepoFacts(repo: repo) }
            if let media = item.preview?.media, !media.isEmpty { MediaGallery(media: media) }
            if let quoted = item.preview?.quoted { QuotedPost(quoted: quoted) }
            if let link = item.preview?.link.flatMap(URL.init(string:)) { LinkCard(url: link) }
            HStack(spacing: 14) {
                if let link = item.link {
                    Button("Open on \(item.host ?? "the web") \u{2197}") { Platform.open(link) }
                        .buttonStyle(.plain).foregroundStyle(.tint)
                        .help("Open the original")
                }
                if let likes = item.preview?.likes, likes > 0 {
                    Label(likes.formatted(.number.notation(.compactName)), systemImage: "heart")
                        .foregroundStyle(.secondary)
                }
            }
            .font(.subheadline)
        }
        .padding(20)
        .overlay { RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(.separator, lineWidth: 1) }
    }
}

// MARK: - Rich content

struct MediaGallery: View {
    let media: [Preview.Media]

    var body: some View {
        if media.count == 1, let only = media.first {
            tile(only)
                .aspectRatio(max(only.aspect, 0.75), contentMode: .fit)
                .frame(maxHeight: 340)
        } else {
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 6), GridItem(.flexible(), spacing: 6)], spacing: 6) {
                ForEach(Array(media.enumerated()), id: \.offset) { _, item in
                    tile(item).frame(height: media.count == 2 ? 220 : 150)
                }
            }
        }
    }

    private func tile(_ item: Preview.Media) -> some View {
        AsyncImage(url: item.imageURL, transaction: Transaction(animation: .easeOut(duration: 0.2))) { phase in
            switch phase {
            case let .success(image): image.resizable().scaledToFill()
            case .failure: Image(systemName: "photo").font(.title).foregroundStyle(.tertiary)
            default: Rectangle().fill(.quaternary.opacity(0.5))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .clipShape(.rect(cornerRadius: 14, style: .continuous))
        .overlay { RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(.separator.opacity(0.4), lineWidth: 0.5) }
        .overlay {
            if item.isVideo {
                Image(systemName: "play.fill")
                    .font(.title2)
                    .foregroundStyle(.white)
                    .frame(width: 52, height: 52)
                    .background(.black.opacity(0.45), in: .circle)
            }
        }
    }
}

struct QuotedPost: View {
    let quoted: Preview.Quoted

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text(quoted.name ?? quoted.author ?? "").font(.subheadline.weight(.semibold)).lineLimit(1)
                if let author = quoted.author { Text(author).font(.subheadline).foregroundStyle(.secondary).lineLimit(1) }
            }
            Text(quoted.text ?? "")
                .font(.system(size: 15))
                .foregroundStyle(.primary.opacity(0.85))
                .lineLimit(6)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .overlay { RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(.separator, lineWidth: 1) }
    }
}

struct RepoFacts: View {
    let repo: Preview.Repo

    var body: some View {
        HStack(spacing: 14) {
            if let language = repo.language {
                Label { Text(language) } icon: { Circle().fill(.tint).frame(width: 8, height: 8) }
            }
            if let stars = repo.stars {
                Label(stars.formatted(.number.notation(.compactName)), systemImage: "star")
            }
            ForEach((repo.topics ?? []).prefix(3), id: \.self) { topic in
                Text(topic)
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Color.accentColor.opacity(0.1), in: .capsule)
                    .foregroundStyle(.tint)
            }
        }
        .font(.subheadline)
        .foregroundStyle(.secondary)
    }
}

/// Native link preview (title, site, image) via LinkPresentation, the same data Messages uses.
struct LinkCard: View {
    let url: URL
    @State private var title: String?
    @State private var host: String?
    @State private var image: PlatformImage?
    @State private var loaded = false
    @MainActor private static var cache: [URL: (title: String?, host: String?, image: PlatformImage?)] = [:]

    var body: some View {
        HStack(spacing: 0) {
            if let image {
                Image(platformImage: image)
                    .resizable()
                    .scaledToFill()
                    .frame(width: 120, height: 84)
                    .clipped()
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(title ?? (loaded ? "Linked page" : "Loading preview…"))
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(2)
                Text(host ?? url.host() ?? "")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 14)
            Spacer(minLength: 0)
        }
        .frame(minHeight: 64)
        .background(.quaternary.opacity(0.35))
        .clipShape(.rect(cornerRadius: 14, style: .continuous))
        .overlay { RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(.separator.opacity(0.5), lineWidth: 0.5) }
        .contentShape(.rect)
        .onTapGesture { Platform.open(url) }
        .task(id: url) { await load() }
    }

    private func load() async {
        if let cached = Self.cache[url] {
            (title, host, image, loaded) = (cached.title, cached.host, cached.image, true)
            return
        }
        let provider = LPMetadataProvider()
        provider.timeout = 8
        guard let metadata = try? await provider.startFetchingMetadata(for: url) else {
            loaded = true
            return
        }
        let finalHost = metadata.url?.host().map { $0.hasPrefix("www.") ? String($0.dropFirst(4)) : $0 }
        var fetched: PlatformImage?
        if let imageProvider = metadata.imageProvider {
            fetched = await withCheckedContinuation { continuation in
                _ = imageProvider.loadObject(ofClass: PlatformImage.self) { object, _ in
                    continuation.resume(returning: object as? PlatformImage)
                }
            }
        }
        Self.cache[url] = (metadata.title, finalHost, fetched)
        (title, host, image, loaded) = (metadata.title, finalHost, fetched, true)
    }
}

// MARK: - Small pieces

struct Avatar: View {
    let seed: String

    var body: some View {
        // hashValue is reseeded every launch; this keeps each author's color stable.
        let hue = Double(seed.unicodeScalars.reduce(7) { ($0 &* 31 &+ Int($1.value)) % 360 }) / 360
        Circle()
            .fill(LinearGradient(colors: [Color(hue: hue, saturation: 0.45, brightness: 0.95),
                                          Color(hue: hue, saturation: 0.65, brightness: 0.75)],
                                 startPoint: .topLeading, endPoint: .bottomTrailing))
            .frame(width: 42, height: 42)
            .overlay {
                Text(initial)
                    .font(.system(size: 18, weight: .semibold, design: .rounded))
                    .foregroundStyle(.white)
            }
    }

    private var initial: String {
        seed.first { $0.isLetter || $0.isNumber }.map { String($0).uppercased() } ?? "•"
    }
}

extension Decision {
    var color: Color {
        switch self {
        case .reject: .red
        case .doIt: .blue
        case .read: .orange
        }
    }
}
