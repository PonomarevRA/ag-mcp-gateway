import AppKit

enum GatewayMark {
    /// Draw the 24-unit PCB mark in a square. The menu bar uses only the template silhouette.
    static func draw(in bounds: NSRect, template: Bool) {
        let scale = bounds.width / 24
        func point(_ x: CGFloat, _ y: CGFloat) -> NSPoint {
            NSPoint(x: bounds.minX + x * scale, y: bounds.maxY - y * scale)
        }

        if !template {
            NSColor(srgbRed: 16 / 255, green: 41 / 255, blue: 50 / 255, alpha: 1).setFill()
            NSBezierPath(roundedRect: bounds, xRadius: 5.3 * scale, yRadius: 5.3 * scale).fill()
        }

        let cyan = template ? NSColor.black : NSColor(srgbRed: 119 / 255, green: 220 / 255, blue: 236 / 255, alpha: 1)
        let mint = template ? NSColor.black : NSColor(srgbRed: 160 / 255, green: 240 / 255, blue: 218 / 255, alpha: 1)
        let lanes: [(input: CGFloat, bend: CGFloat, color: NSColor)] = [
            (4.5, 8.5, cyan), (12, 12, mint), (19.5, 15.5, cyan),
        ]

        // The bends share a 3.5-unit pitch so the paths stay separate at favicon scale.
        for (input, bend, color) in lanes {
            let trace = NSBezierPath()
            trace.lineWidth = 1.3 * scale
            trace.lineCapStyle = .round
            trace.lineJoinStyle = .round
            trace.move(to: point(input, 4.5))
            trace.line(to: point(input, 7.5))
            trace.line(to: point(bend, 11.5))
            trace.line(to: point(bend, 12))
            trace.curve(to: point(bend - 1.25, 16),
                        controlPoint1: point(bend, 13.5), controlPoint2: point(bend - 1.25, 14.5))
            trace.curve(to: point(bend + 1.75, 19.5),
                        controlPoint1: point(bend - 1.25, 17.25), controlPoint2: point(bend + 0.4, 18))
            color.setStroke()
            trace.stroke()
        }

        // Convert each pad's bottom edge from top-down SVG units to AppKit coordinates.
        func fillPad(left: CGFloat, bottom: CGFloat, width: CGFloat, height: CGFloat,
                     radius: CGFloat, color: NSColor) {
            let frame = NSRect(origin: point(left, bottom), size: NSSize(width: width * scale, height: height * scale))
            color.setFill()
            NSBezierPath(roundedRect: frame, xRadius: radius * scale, yRadius: radius * scale).fill()
        }
        for (input, _, color) in lanes {
            fillPad(left: input - 1.5, bottom: 6, width: 3, height: 3, radius: 0.45, color: color)
        }
        fillPad(left: 9, bottom: 21, width: 9.5, height: 2, radius: 0.55, color: mint)
    }

    static func trayImage() -> NSImage {
        let image = NSImage(size: NSSize(width: 20, height: 20), flipped: false) {
            draw(in: $0, template: true)
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "ag-mcp-gateway"
        return image
    }

    static func writeIcon(to path: String, size: Int) throws {
        guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                                             bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                             colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
              let context = NSGraphicsContext(bitmapImageRep: bitmap) else { throw CocoaError(.fileWriteUnknown) }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        draw(in: NSRect(x: 0, y: 0, width: size, height: size), template: false)
        NSGraphicsContext.restoreGraphicsState()
        guard let png = bitmap.representation(using: .png, properties: [:]) else {
            throw CocoaError(.fileWriteUnknown)
        }
        try png.write(to: URL(fileURLWithPath: path))
    }
}
