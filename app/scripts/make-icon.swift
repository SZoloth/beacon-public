// Renders the app icon: a beacon glyph on an orange-to-coral gradient.
// Usage: swift make-icon.swift <output.iconset>      Mac: Big Sur–style rounded square with a shadow, all sizes
//        swift make-icon.swift --ios <output.png>    iPhone: one 1024px full-bleed square; iOS rounds the corners
import AppKit

let ios = CommandLine.arguments[1] == "--ios"
let output = URL(fileURLWithPath: CommandLine.arguments.last!)
if !ios { try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true) }

func render(_ pixels: Int) -> Data {
    let size = CGFloat(pixels)
    let image = NSImage(size: NSSize(width: size, height: size), flipped: false) { _ in
        let inset = ios ? 0 : size * 0.1
        let tile = NSRect(x: inset, y: inset, width: size - inset * 2, height: size - inset * 2)
        let path = ios ? NSBezierPath(rect: tile) : NSBezierPath(roundedRect: tile, xRadius: tile.width * 0.225, yRadius: tile.width * 0.225)

        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowColor = NSColor.black.withAlphaComponent(0.28)
        shadow.shadowBlurRadius = size * 0.025
        shadow.shadowOffset = NSSize(width: 0, height: -size * 0.012)
        if !ios { shadow.set() }
        NSColor.black.setFill()
        path.fill()
        NSGraphicsContext.restoreGraphicsState()

        NSGradient(colors: [NSColor(calibratedRed: 1.0, green: 0.62, blue: 0.25, alpha: 1),
                            NSColor(calibratedRed: 0.93, green: 0.27, blue: 0.38, alpha: 1)])!
            .draw(in: path, angle: -90)

        let config = NSImage.SymbolConfiguration(pointSize: tile.width * 0.5, weight: .semibold)
            .applying(.init(paletteColors: [.white]))
        if let symbol = NSImage(systemSymbolName: "light.beacon.max.fill", accessibilityDescription: nil)?
            .withSymbolConfiguration(config) {
            let glyph = symbol.size
            symbol.draw(in: NSRect(x: tile.midX - glyph.width / 2, y: tile.midY - glyph.height / 2,
                                   width: glyph.width, height: glyph.height))
        }
        return true
    }
    let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
    return bitmap.representation(using: .png, properties: [:])!
}

if ios {
    try render(1024).write(to: output)
    exit(0)
}

for base in [16, 32, 128, 256, 512] {
    try render(base).write(to: output.appendingPathComponent("icon_\(base)x\(base).png"))
    try render(base * 2).write(to: output.appendingPathComponent("icon_\(base)x\(base)@2x.png"))
}
