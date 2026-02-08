import Foundation
import AppKit
import QuickLookThumbnailing

guard CommandLine.arguments.count == 3 else {
    fputs("Usage: thumbnail <input-file> <output.png>\n", stderr)
    exit(1)
}

let url = URL(fileURLWithPath: CommandLine.arguments[1])
let outputPath = CommandLine.arguments[2]
let size = CGSize(width: 512, height: 512)
let request = QLThumbnailGenerator.Request(fileAt: url, size: size, scale: 2.0, representationTypes: .all)

let semaphore = DispatchSemaphore(value: 0)
var exitCode: Int32 = 0

QLThumbnailGenerator.shared.generateBestRepresentation(for: request) { thumbnail, error in
    if let error = error {
        fputs("Error: \(error)\n", stderr)
        exitCode = 1
        semaphore.signal()
        return
    }
    if let thumb = thumbnail {
        let bitmapRep = NSBitmapImageRep(cgImage: thumb.cgImage)
        if let data = bitmapRep.representation(using: .png, properties: [:]) {
            do {
                try data.write(to: URL(fileURLWithPath: outputPath))
                print("Saved thumbnail to \(outputPath)")
            } catch {
                fputs("Error writing file: \(error)\n", stderr)
                exitCode = 1
            }
        } else {
            fputs("Error: failed to create PNG representation\n", stderr)
            exitCode = 1
        }
    }
    semaphore.signal()
}

semaphore.wait()
exit(exitCode)
