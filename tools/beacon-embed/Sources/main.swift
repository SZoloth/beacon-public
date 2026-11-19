// beacon-embed: search by meaning and find similar pictures, on-device.
//
//   beacon-embed index --db PATH          JSON lines on stdin: {"id": 1, "image": "https://...", "words": "..."}
//                                         For a picture: its SigLIP 2 vector (text_vec; text and pictures
//                                         share one space, so a description finds the pictures that show it)
//                                         and Vision's fingerprint (image_vec, for look-alikes). For words: the
//                                         SigLIP 2 text vector of what the save says (words_vec), so saves
//                                         without a picture are found by meaning too. Either may be missing;
//                                         what a job leaves out stays as it was. Stored in item_vectors.
//                                         Prints one JSON summary line.
//   beacon-embed meaning --db PATH --query TEXT [--limit N]   best pictures by meaning: [[id, score], ...]
//   beacon-embed search --db PATH --query TEXT [--limit N]    both meanings from one encode:
//                                         {"pictures": [[id, score], ...], "words": [[id, score], ...]}
//   beacon-embed similar --db PATH --id ID [--limit N]        pictures that look like item ID: [[id, score], ...]
//   beacon-embed serve --db PATH          Stays running (for `beacon serve`): one JSON request per stdin line,
//                                         {"meaning": TEXT}, {"search": TEXT} or {"similar": ID}, plus "limit"; one JSON answer
//                                         per line. The model and vectors stay loaded, so an answer takes
//                                         milliseconds; vectors reload when the index changes.
//
// Vectors are unit length, so a dot product is the cosine similarity.
import Accelerate
import CoreML
import Foundation
import ImageIO
import SQLite3
import Tokenizers
import Vision

let model = "siglip2-8bit+vision-fp-v1"
/// Copied from Mosaic's download (same files); Beacon keeps its own copy so Mosaic can go.
let siglipFolder = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    .appendingPathComponent("Beacon/Models/ViT-B-16-SigLIP2-8bit-v1")

struct Failure: Error, CustomStringConvertible {
    let description: String
}

func argument(_ name: String) -> String? {
    let args = CommandLine.arguments
    guard let index = args.firstIndex(of: name), index + 1 < args.count else { return nil }
    return args[index + 1]
}

func normalized(_ vector: [Float]) -> [Float] {
    var norm: Float = 0
    vDSP_svesq(vector, 1, &norm, vDSP_Length(vector.count))
    norm = norm.squareRoot()
    guard norm > 0 else { return vector }
    return vector.map { $0 / norm }
}

func blob(_ vector: [Float]) -> Data { vector.withUnsafeBufferPointer { Data(buffer: $0) } }

func floats(_ data: Data) -> [Float] {
    data.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
}

// MARK: - SQLite

final class Database {
    let handle: OpaquePointer

    init(path: String) throws {
        var db: OpaquePointer?
        guard sqlite3_open(path, &db) == SQLITE_OK, let db else { throw Failure(description: "Can't open \(path)") }
        handle = db
        sqlite3_busy_timeout(db, 10_000)
        try exec("""
            CREATE TABLE IF NOT EXISTS item_vectors (
                item_id INTEGER PRIMARY KEY, model TEXT NOT NULL,
                text_vec BLOB, image_vec BLOB, words_vec BLOB, indexed_at TEXT NOT NULL)
            """)
        // A table made before words_vec existed gets the column; it fails harmlessly once it's there.
        try? exec("ALTER TABLE item_vectors ADD COLUMN words_vec BLOB")
    }

    deinit { sqlite3_close(handle) }

    func exec(_ sql: String) throws {
        guard sqlite3_exec(handle, sql, nil, nil, nil) == SQLITE_OK else {
            throw Failure(description: String(cString: sqlite3_errmsg(handle)))
        }
    }

    /// A vector left nil keeps what's stored, so reading a save's words doesn't drop its picture's vectors.
    func save(id: Int, text: [Float]?, image: [Float]?, words: [Float]?) throws {
        var statement: OpaquePointer?
        let sql = """
            INSERT INTO item_vectors(item_id, model, text_vec, image_vec, words_vec, indexed_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET model = excluded.model,
                text_vec = coalesce(excluded.text_vec, text_vec), image_vec = coalesce(excluded.image_vec, image_vec),
                words_vec = coalesce(excluded.words_vec, words_vec), indexed_at = excluded.indexed_at
            """
        guard sqlite3_prepare_v2(handle, sql, -1, &statement, nil) == SQLITE_OK else {
            throw Failure(description: String(cString: sqlite3_errmsg(handle)))
        }
        defer { sqlite3_finalize(statement) }
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        sqlite3_bind_int64(statement, 1, Int64(id))
        sqlite3_bind_text(statement, 2, model, -1, transient)
        for (index, vector) in [(3, text), (4, image), (5, words)] {
            if let vector {
                let data = blob(vector)
                _ = data.withUnsafeBytes { sqlite3_bind_blob(statement, Int32(index), $0.baseAddress, Int32(data.count), transient) }
            } else {
                sqlite3_bind_null(statement, Int32(index))
            }
        }
        sqlite3_bind_text(statement, 6, ISO8601DateFormatter().string(from: Date()), -1, transient)
        guard sqlite3_step(statement) == SQLITE_DONE else { throw Failure(description: String(cString: sqlite3_errmsg(handle))) }
    }

    /// Changes whenever another process writes the database, so `serve` reloads vectors only then.
    /// (Free to ask; counting the vectors instead read all 90 MB of them, 100 ms per search.)
    func dataVersion() -> Int64 {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(handle, "PRAGMA data_version", -1, &statement, nil) == SQLITE_OK else { return -1 }
        defer { sqlite3_finalize(statement) }
        return sqlite3_step(statement) == SQLITE_ROW ? sqlite3_column_int64(statement, 0) : -1
    }

    /// Every stored vector of one kind ("text_vec", "image_vec" or "words_vec").
    func vectors(_ column: String) -> [(Int, [Float])] {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(handle, "SELECT item_id, \(column) FROM item_vectors WHERE \(column) IS NOT NULL", -1, &statement, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(statement) }
        var rows: [(Int, [Float])] = []
        while sqlite3_step(statement) == SQLITE_ROW {
            let id = Int(sqlite3_column_int64(statement, 0))
            guard let bytes = sqlite3_column_blob(statement, 1) else { continue }
            rows.append((id, floats(Data(bytes: bytes, count: Int(sqlite3_column_bytes(statement, 1))))))
        }
        return rows
    }
}

// MARK: - Seeing and reading

// MARK: SigLIP 2

struct SigLIP {
    let image: MLModel
    let text: MLModel
    let tokenizer: any Tokenizer
    static let textLength = 64, pad = 0, end = 1

    static func load() async throws -> SigLIP {
        let configuration = MLModelConfiguration()
        // Not the GPU: under load, the GPU path failed an assertion in Metal (MPSGraphTensorData) and
        // killed the long-running helper. The Neural Engine is as fast for these small models.
        configuration.computeUnits = .cpuAndNeuralEngine
        let tokenizer = try await AutoTokenizer.from(modelFolder: siglipFolder, strict: true)
        let image = try MLModel(contentsOf: siglipFolder.appendingPathComponent("ViT-B-16-SigLIP2_image_8bit.mlmodelc"), configuration: configuration)
        let text = try MLModel(contentsOf: siglipFolder.appendingPathComponent("ViT-B-16-SigLIP2_text_8bit.mlmodelc"), configuration: configuration)
        return SigLIP(image: image, text: text, tokenizer: tokenizer)
    }

    func vector(_ output: MLFeatureProvider) throws -> [Float] {
        guard let values = output.featureValue(for: "embedding")?.multiArrayValue, values.count == 768 else {
            throw Failure(description: "unexpected SigLIP output")
        }
        return normalized((0..<values.count).map { values[$0].floatValue })
    }

    /// Lowercased, like SigLIP 2's training text; 64 tokens ending in <eos>, padded.
    func embed(text: String) throws -> [Float] {
        var ids = tokenizer.encode(text: text.lowercased())
        if ids.count >= Self.textLength { ids = Array(ids.prefix(Self.textLength - 1)) + [Self.end] }
        else if ids.last != Self.end { ids.append(Self.end) }
        ids += Array(repeating: Self.pad, count: Self.textLength - ids.count)
        let input = try MLMultiArray(shape: [1, NSNumber(value: Self.textLength)], dataType: .int32)
        for (index, id) in ids.enumerated() { input[index] = NSNumber(value: Int32(id)) }
        return try vector(self.text.prediction(from: MLDictionaryFeatureProvider(dictionary: ["text": MLFeatureValue(multiArray: input)])))
    }

    func embed(picture: CGImage) throws -> [Float] {
        let pixels = try MLFeatureValue(cgImage: picture, pixelsWide: 224, pixelsHigh: 224,
                                        pixelFormatType: kCVPixelFormatType_32ARGB, options: nil)
        return try vector(image.prediction(from: MLDictionaryFeatureProvider(dictionary: ["image": pixels])))
    }
}

func decode(_ data: Data) -> CGImage? {
    guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
    return CGImageSourceCreateThumbnailAtIndex(source, 0, [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: 512,
    ] as CFDictionary)
}

/// Vision's visual fingerprint: what similar pictures are matched on.
func fingerprint(_ picture: CGImage) throws -> [Float] {
    let request = VNGenerateImageFeaturePrintRequest()
    try VNImageRequestHandler(cgImage: picture).perform([request])
    guard let observation = request.results?.first, observation.elementType == .float else {
        throw Failure(description: "no fingerprint")
    }
    return normalized(observation.data.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) })
}

/// A smaller copy of an X picture is plenty for both models.
func small(_ url: URL) -> URL {
    guard url.host() == "pbs.twimg.com", var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return url }
    parts.queryItems = [URLQueryItem(name: "format", value: "jpg"), URLQueryItem(name: "name", value: "small")]
    return parts.url ?? url
}

struct Job: Decodable {
    let id: Int
    let image: String?
    let words: String?
}

func index(database: Database, siglip: SigLIP) async throws {
    let jobs = FileHandle.standardInput.readDataToEndOfFile()
        .split(separator: UInt8(ascii: "\n"))
        .compactMap { try? JSONDecoder().decode(Job.self, from: Data($0)) }
    var indexed = 0, failed = 0, read = 0
    let session = URLSession(configuration: .ephemeral)
    // Eight pictures at a time; SQLite writes stay on this task.
    for batch in stride(from: 0, to: jobs.count, by: 8).map({ Array(jobs[$0..<min($0 + 8, jobs.count)]) }) {
        // Downloads in parallel; the models run one picture at a time.
        let downloads = await withTaskGroup(of: (Job, CGImage?).self) { group in
            for job in batch {
                group.addTask {
                    guard let raw = job.image, let url = URL(string: raw) else { return (job, nil) }
                    if url.isFileURL { return (job, (try? Data(contentsOf: url)).flatMap(decode)) }  // a picture you added
                    guard let (data, response) = try? await session.data(from: small(url)),
                          (response as? HTTPURLResponse)?.statusCode == 200 else { return (job, nil) }
                    return (job, decode(data))
                }
            }
            return await group.reduce(into: []) { $0.append($1) }
        }
        for (job, picture) in downloads {
            let meaning = picture.flatMap { try? siglip.embed(picture: $0) }
            let look = picture.flatMap { try? fingerprint($0) }
            if job.image != nil, meaning == nil { failed += 1 }
            var words: [Float]? = nil
            if let text = job.words, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                words = try? siglip.embed(text: text)
            }
            if words != nil { read += 1 }
            try database.save(id: job.id, text: meaning, image: look, words: words)
            indexed += 1
        }
    }
    let summary: [String: Int] = ["indexed": indexed, "pictures_failed": failed, "words_read": read]
    print(String(data: try JSONSerialization.data(withJSONObject: summary), encoding: .utf8)!)
}

func rank(_ query: [Float], against rows: [(Int, [Float])], limit: Int, skip: Int? = nil) -> [[Any]] {
    var scored: [(Int, Float)] = []
    scored.reserveCapacity(rows.count)
    for (id, vector) in rows where id != skip && vector.count == query.count {
        var dot: Float = 0
        vDSP_dotpr(query, 1, vector, 1, &dot, vDSP_Length(query.count))
        scored.append((id, dot))
    }
    return scored.sorted { $0.1 > $1.1 }.prefix(limit).map { [$0.0, Double($0.1)] }
}

func emit(_ value: Any) {
    print(String(data: try! JSONSerialization.data(withJSONObject: value), encoding: .utf8)!)
    fflush(stdout)  // a pipe is block-buffered; `serve` answers must arrive line by line
}

/// `serve`: answer meaning and similar requests from stdin until it closes.
func serve(database: Database, siglip: SigLIP) {
    var version: Int64?
    var texts: [(Int, [Float])] = [], images: [(Int, [Float])] = [], words: [(Int, [Float])] = []
    while let line = readLine() {
        do {
            guard let request = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] else {
                throw Failure(description: "send a JSON object")
            }
            let now = database.dataVersion()
            if now != version {
                texts = database.vectors("text_vec")
                images = database.vectors("image_vec")
                words = database.vectors("words_vec")
                version = now
            }
            let limit = request["limit"] as? Int ?? 200
            if let query = request["meaning"] as? String, !query.trimmingCharacters(in: .whitespaces).isEmpty {
                emit(rank(try siglip.embed(text: query), against: texts, limit: limit))
            } else if let query = request["search"] as? String, !query.trimmingCharacters(in: .whitespaces).isEmpty {
                let vector = try siglip.embed(text: query)
                emit(["pictures": rank(vector, against: texts, limit: limit), "words": rank(vector, against: words, limit: limit)])
            } else if let id = request["similar"] as? Int {
                emit(images.first { $0.0 == id }.map { rank($0.1, against: images, limit: limit, skip: id) } ?? [])
            } else {
                throw Failure(description: "send {\"meaning\": text}, {\"search\": text} or {\"similar\": id}")
            }
        } catch {
            emit(["error": "\(error)"])
        }
    }
}

// MARK: - Main

do {
    guard let command = CommandLine.arguments.dropFirst().first, let path = argument("--db") else {
        throw Failure(description: "usage: beacon-embed index|meaning|search|similar --db PATH ...")
    }
    let database = try Database(path: path)
    let limit = argument("--limit").flatMap(Int.init) ?? 200
    switch command {
    case "index":
        try await index(database: database, siglip: SigLIP.load())
    case "meaning":
        guard let query = argument("--query"), !query.trimmingCharacters(in: .whitespaces).isEmpty else {
            throw Failure(description: "meaning needs --query with some words")
        }
        let vector = try await SigLIP.load().embed(text: query)
        emit(rank(vector, against: database.vectors("text_vec"), limit: limit))
    case "search":
        guard let query = argument("--query"), !query.trimmingCharacters(in: .whitespaces).isEmpty else {
            throw Failure(description: "search needs --query with some words")
        }
        let vector = try await SigLIP.load().embed(text: query)
        emit(["pictures": rank(vector, against: database.vectors("text_vec"), limit: limit),
              "words": rank(vector, against: database.vectors("words_vec"), limit: limit)])
    case "serve":
        serve(database: database, siglip: try await SigLIP.load())
    case "similar":
        guard let id = argument("--id").flatMap(Int.init) else { throw Failure(description: "similar needs --id") }
        let rows = database.vectors("image_vec")
        guard let anchor = rows.first(where: { $0.0 == id })?.1 else { emit([]); break }
        emit(rank(anchor, against: rows, limit: limit, skip: id))
    default:
        throw Failure(description: "unknown command \(command)")
    }
} catch {
    FileHandle.standardError.write(Data("beacon-embed: \(error)\n".utf8))
    exit(1)
}
