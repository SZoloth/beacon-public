#if os(macOS)
import AppKit
import CoreImage.CIFilterBuiltins
import SwiftUI

/// Edit › Pair iPhone…: a QR code the iPhone's Camera opens in Beacon, carrying this Mac's
/// Tailscale address and the phone's access key.
struct PairMenuButton: View {
    @Environment(\.openWindow) private var openWindow
    var body: some View { Button("Pair iPhone…") { openWindow(id: "pair") } }
}

struct PairView: View {
    @State private var state: PairState = .loading

    var body: some View {
        VStack(spacing: 16) {
            switch state {
            case .loading:
                ProgressView().frame(width: 260, height: 260)
            case .ready(let image, let host):
                Image(nsImage: image)
                    .interpolation(.none)
                    .resizable()
                    .frame(width: 260, height: 260)
                    .accessibilityLabel("Pairing code")
                Text("Scan with your iPhone's Camera")
                    .font(.system(size: 17, weight: .semibold))
                Text(host).font(.system(size: 13)).foregroundStyle(.secondary)
            case .failed(let message):
                Text(message)
                    .frame(width: 260)
                    .multilineTextAlignment(.center)
            }
        }
        .padding(28)
        .task { state = await PairCode.make() }
    }
}

enum PairState {
    case loading
    case ready(NSImage, String)
    case failed(String)
}

enum PairCode {
    static func make() async -> PairState {
        guard let host = await tailscaleName() else {
            return .failed("Tailscale isn't running on this Mac, so the phone has no address to reach. Open Tailscale and try again.")
        }
        guard let key = PhoneKey.read() else {
            return .failed("Beacon's server hasn't made an access key yet. Its launchd job starts it; try again in a moment.")
        }
        guard let server = URL(string: "http://\(host):8765"),
              let link = BeaconLink.pairURL(server: server, key: key),
              let image = qrImage(link.absoluteString) else {
            return .failed("Couldn't make the pairing code.")
        }
        return .ready(image, host)
    }

    /// This Mac's MagicDNS name, from whichever Tailscale CLI is installed.
    static func tailscaleName() async -> String? {
        await Task.detached {
            let candidates = ["/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
                              "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
            for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: path)
                process.arguments = ["status", "--json"]
                let pipe = Pipe()
                process.standardOutput = pipe
                process.standardError = FileHandle.nullDevice
                guard (try? process.run()) != nil else { continue }
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                guard process.terminationStatus == 0,
                      let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let me = json["Self"] as? [String: Any],
                      let name = (me["DNSName"] as? String)?.trimmingCharacters(in: CharacterSet(charactersIn: ".")),
                      !name.isEmpty else { continue }
                return name
            }
            return nil
        }.value
    }

    static func qrImage(_ text: String) -> NSImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(text.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 8, y: 8)),
              let cgImage = CIContext().createCGImage(output, from: output.extent) else { return nil }
        return NSImage(cgImage: cgImage, size: NSSize(width: output.extent.width, height: output.extent.height))
    }
}
#endif
