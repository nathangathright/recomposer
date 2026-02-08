import AppKit

// Reframe a bitmap image within a canvas at a specified position and size.
// Usage: reframe <input> <output> <canvas_w> <canvas_h> <x> <y> <w> <h>
//
// Resizes the source image to (w x h) and places it at position (x, y)
// within a transparent (canvas_w x canvas_h) canvas.
// Coordinates use bottom-left origin (matching CoreGraphics / asset catalog).
// Output is always at 1x pixel resolution regardless of display scale.

guard CommandLine.arguments.count == 9 else {
    fputs("usage: reframe <input> <output> <canvas_w> <canvas_h> <x> <y> <w> <h>\n", stderr)
    exit(1)
}

let inputPath = CommandLine.arguments[1]
let outputPath = CommandLine.arguments[2]
let canvasW = Int(CommandLine.arguments[3])!
let canvasH = Int(CommandLine.arguments[4])!
let posX = Int(CommandLine.arguments[5])!
let posY = Int(CommandLine.arguments[6])!
let dispW = Int(CommandLine.arguments[7])!
let dispH = Int(CommandLine.arguments[8])!

guard let srcImage = NSImage(contentsOfFile: inputPath) else {
    fputs("error: failed to load \(inputPath)\n", stderr)
    exit(1)
}

// Create a pixel-exact bitmap at 1x resolution (avoids Retina @2x scaling)
guard let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: canvasW,
    pixelsHigh: canvasH,
    bitsPerSample: 8,
    samplesPerPixel: 4,
    hasAlpha: true,
    isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0,
    bitsPerPixel: 0
) else {
    fputs("error: failed to create bitmap\n", stderr)
    exit(1)
}

// Set size to match pixels (1x, 72 DPI)
rep.size = NSSize(width: canvasW, height: canvasH)

guard let ctx = NSGraphicsContext(bitmapImageRep: rep) else {
    fputs("error: failed to create graphics context\n", stderr)
    exit(1)
}

let prev = NSGraphicsContext.current
NSGraphicsContext.current = ctx

// Clear to transparent
NSColor.clear.set()
NSRect(x: 0, y: 0, width: canvasW, height: canvasH).fill()

// Draw the source image at the specified position and size.
// Both the catalog and AppKit use bottom-left origin.
srcImage.draw(
    in: NSRect(x: CGFloat(posX), y: CGFloat(posY),
               width: CGFloat(dispW), height: CGFloat(dispH)),
    from: .zero,
    operation: .sourceOver,
    fraction: 1.0
)

NSGraphicsContext.current = prev

guard let png = rep.representation(using: .png, properties: [:]) else {
    fputs("error: failed to encode PNG\n", stderr)
    exit(1)
}

do {
    try png.write(to: URL(fileURLWithPath: outputPath))
} catch {
    fputs("error: \(error)\n", stderr)
    exit(1)
}
