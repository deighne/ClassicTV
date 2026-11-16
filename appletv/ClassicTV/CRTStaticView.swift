import SwiftUI
import UIKit

struct CRTStaticView: View {
    private static let frames: [UIImage] = {
        let w = 480
        let h = 270
        let colorSpace = CGColorSpaceCreateDeviceGray()
        return (0..<4).map { _ in
            var bytes = [UInt8](repeating: 0, count: w * h)
            for y in 0..<h {
                let scan: Double = (y & 1) == 1 ? 0.88 : 1.0
                var prev = Int.random(in: 10...240)
                let rowOff = y * w
                for x in 0..<w {
                    let r = Int.random(in: 0..<100)
                    let v: Int
                    if r < 4 {
                        v = Int.random(in: 230...255)
                    } else if r < 8 {
                        v = Int.random(in: 0...25)
                    } else {
                        let raw = Int.random(in: 15...235)
                        v = Int(0.82 * Double(raw) + 0.18 * Double(prev))
                    }
                    prev = v
                    bytes[rowOff + x] = UInt8(max(0, min(255, Int(Double(v) * scan))))
                }
            }
            let data = Data(bytes)
            guard let provider = CGDataProvider(data: data as CFData),
                  let cg = CGImage(
                    width: w,
                    height: h,
                    bitsPerComponent: 8,
                    bitsPerPixel: 8,
                    bytesPerRow: w,
                    space: colorSpace,
                    bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
                    provider: provider,
                    decode: nil,
                    shouldInterpolate: false,
                    intent: .defaultIntent
                  ) else {
                return UIImage()
            }
            return UIImage(cgImage: cg)
        }
    }()

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.055)) { context in
            let idx = Int(context.date.timeIntervalSinceReferenceDate / 0.055) % Self.frames.count
            Image(uiImage: Self.frames[abs(idx)])
                .interpolation(.none)
                .resizable()
                .ignoresSafeArea()
        }
    }
}
