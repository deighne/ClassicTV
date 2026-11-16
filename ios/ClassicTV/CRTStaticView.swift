import SwiftUI
import UIKit

struct CRTStaticView: View {
    private let frames: [UIImage] = CRTStaticView.buildFrames(count: 4, width: 480, height: 270)

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.055)) { ctx in
            let idx = Int(ctx.date.timeIntervalSinceReferenceDate / 0.055) % frames.count
            Image(uiImage: frames[abs(idx)])
                .resizable()
                .interpolation(.none)
                .ignoresSafeArea()
                .opacity(0.92)
        }
    }

    private static func buildFrames(count: Int, width: Int, height: Int) -> [UIImage] {
        var out: [UIImage] = []
        out.reserveCapacity(count)
        let colorSpace = CGColorSpaceCreateDeviceGray()

        for _ in 0..<count {
            var pixels = [UInt8](repeating: 0, count: width * height)
            for y in 0..<height {
                let scan: Double = (y & 1) == 1 ? 0.86 : 1.0
                var prev = Int.random(in: 20...235)
                let rowOffset = y * width
                for x in 0..<width {
                    let r = Double.random(in: 0..<1)
                    let v: Int
                    if r < 0.04 {
                        v = Int.random(in: 230...255)
                    } else if r < 0.08 {
                        v = Int.random(in: 0...25)
                    } else {
                        let raw = Int.random(in: 15...235)
                        v = Int(0.82 * Double(raw) + 0.18 * Double(prev))
                    }
                    prev = v
                    pixels[rowOffset + x] = UInt8(clamping: Int(Double(v) * scan))
                }
            }

            let data = Data(pixels)
            if let provider = CGDataProvider(data: data as CFData),
               let cg = CGImage(
                   width: width,
                   height: height,
                   bitsPerComponent: 8,
                   bitsPerPixel: 8,
                   bytesPerRow: width,
                   space: colorSpace,
                   bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
                   provider: provider,
                   decode: nil,
                   shouldInterpolate: false,
                   intent: .defaultIntent
               ) {
                out.append(UIImage(cgImage: cg))
            }
        }
        return out.isEmpty ? [UIImage()] : out
    }
}
