import Foundation
import AppKit
import QuickLookThumbnailing
import WebKit

guard CommandLine.arguments.count == 3 else {
    fputs("Usage: thumbnail <input-file> <output.png>\n", stderr)
    exit(1)
}

let url = URL(fileURLWithPath: CommandLine.arguments[1])
let outputPath = CommandLine.arguments[2]

// ---------------------------------------------------------------------------
// Helper: save a CGImage as PNG
// ---------------------------------------------------------------------------

func savePNG(_ cgImage: CGImage) -> Bool {
    let rep = NSBitmapImageRep(cgImage: cgImage)
    guard let data = rep.representation(using: .png, properties: [:]) else {
        fputs("Error: failed to create PNG representation\n", stderr)
        return false
    }
    do {
        try data.write(to: URL(fileURLWithPath: outputPath))
        return true
    } catch {
        fputs("Error writing file: \(error)\n", stderr)
        return false
    }
}

// ---------------------------------------------------------------------------
// SVG path: render via WebKit (supports CSS custom properties, SVG filters,
// @media queries that QuickLook cannot handle)
// ---------------------------------------------------------------------------

if url.pathExtension.lowercased() == "svg" {

    class SVGNavigationDelegate: NSObject, WKNavigationDelegate {
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            let config = WKSnapshotConfiguration()
            config.snapshotWidth = NSNumber(value: 1024)
            webView.takeSnapshot(with: config) { image, error in
                if let error = error {
                    fputs("Error taking snapshot: \(error)\n", stderr)
                    exit(1)
                }
                guard let nsImage = image,
                      let cgImage = nsImage.cgImage(forProposedRect: nil,
                                                     context: nil, hints: nil) else {
                    fputs("Error: snapshot produced no image\n", stderr)
                    exit(1)
                }
                exit(savePNG(cgImage) ? 0 : 1)
            }
        }

        func webView(_ webView: WKWebView,
                      didFailProvisionalNavigation navigation: WKNavigation!,
                      withError error: Error) {
            fputs("Error loading SVG: \(error)\n", stderr)
            exit(1)
        }

        func webView(_ webView: WKWebView,
                      didFail navigation: WKNavigation!,
                      withError error: Error) {
            fputs("Error rendering SVG: \(error)\n", stderr)
            exit(1)
        }
    }

    guard let svgContent = try? String(contentsOf: url, encoding: .utf8) else {
        fputs("Error: cannot read \(url.path)\n", stderr)
        exit(1)
    }

    // Wrap SVG in a minimal HTML page that forces 1024x1024 layout with
    // no margins, so the viewBox fills the viewport exactly.
    let html = """
    <!DOCTYPE html>
    <html style="margin:0;padding:0;overflow:hidden">
    <head><style>svg{display:block;width:1024px;height:1024px}</style></head>
    <body style="margin:0;padding:0">
    \(svgContent)
    </body></html>
    """

    let delegate = SVGNavigationDelegate()
    let webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1024, height: 1024))
    webView.navigationDelegate = delegate
    webView.loadHTMLString(html, baseURL: url.deletingLastPathComponent())

    // Keep the delegate alive (navigationDelegate is weak) and pump the
    // run loop until the snapshot completion handler calls exit().
    withExtendedLifetime(delegate) {
        RunLoop.main.run()
    }
}

// ---------------------------------------------------------------------------
// Non-SVG path: render via QuickLook (for .icon bundles, .app, etc.)
// ---------------------------------------------------------------------------

let size = CGSize(width: 512, height: 512)
let request = QLThumbnailGenerator.Request(
    fileAt: url, size: size, scale: 2.0, representationTypes: .all)

let semaphore = DispatchSemaphore(value: 0)
var exitCode: Int32 = 0

QLThumbnailGenerator.shared.generateBestRepresentation(for: request) { thumbnail, error in
    if let error = error {
        fputs("Error: \(error)\n", stderr)
        exitCode = 1
    } else if let thumb = thumbnail {
        if savePNG(thumb.cgImage) {
            print("Saved thumbnail to \(outputPath)")
        } else {
            exitCode = 1
        }
    }
    semaphore.signal()
}

semaphore.wait()
exit(exitCode)
