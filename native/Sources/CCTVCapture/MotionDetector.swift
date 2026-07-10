@preconcurrency import CoreImage
import CoreVideo
import Foundation
@preconcurrency import VideoToolbox

struct MotionResult: Sendable {
    let candidate: Bool
    let score: Double
    let confidence: Double
    let boundingBox: NormalizedRect?
}

private final class MotionRequestBox: @unchecked Sendable {
    let semaphore = DispatchSemaphore(value: 0)
    let lock = NSLock()
    var result = MotionResult(candidate: false, score: 0, confidence: 0, boundingBox: nil)
}

actor MotionDetector {
    private let width = 512
    private let height = 384
    private let context: CIContext
    private let session: __VTMotionEstimationSession
    private var pool: CVPixelBufferPool?
    private var previous: CVPixelBuffer?

    // Existing 1024x768 ROI normalized once. It intentionally retains the current
    // near-full-frame coverage while excluding the small margins outside the polygon.
    private let roi: [CGPoint] = [
        CGPoint(x: 0.012, y: 0.007), CGPoint(x: 0.034, y: 0.005),
        CGPoint(x: 0.122, y: 0.013), CGPoint(x: 0.178, y: 0.072),
        CGPoint(x: 0.299, y: 0.076), CGPoint(x: 0.515, y: 0.082),
        CGPoint(x: 0.732, y: 0.117), CGPoint(x: 0.873, y: 0.013),
        CGPoint(x: 0.985, y: 0.018), CGPoint(x: 0.999, y: 0.200),
        CGPoint(x: 0.993, y: 0.918), CGPoint(x: 0.941, y: 0.991),
        CGPoint(x: 0.478, y: 0.983), CGPoint(x: 0.087, y: 0.980),
        CGPoint(x: 0.009, y: 0.908), CGPoint(x: 0.012, y: 0.012),
    ]

    init(context: CIContext) throws {
        self.context = context
        let options: [CFString: Any] = [
            kVTMotionEstimationSessionCreationOption_MotionVectorSize: 16,
            kVTMotionEstimationSessionCreationOption_UseMultiPassSearch: true,
            kVTMotionEstimationSessionCreationOption_Label: "CCTV true-motion detector",
        ]
        var motionSession: __VTMotionEstimationSession?
        let motionStatus = __VTMotionEstimationSessionCreate(
            nil, options as CFDictionary, UInt32(width), UInt32(height), &motionSession
        )
        guard motionStatus == noErr, let motionSession else {
            throw MotionError.session(motionStatus)
        }
        self.session = motionSession
        let attributes: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey: width,
            kCVPixelBufferHeightKey: height,
            kCVPixelBufferMetalCompatibilityKey: true,
            kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary,
        ]
        var created: CVPixelBufferPool?
        let status = CVPixelBufferPoolCreate(nil, nil, attributes as CFDictionary, &created)
        guard status == kCVReturnSuccess, let created else {
            throw MotionError.pixelBufferPool(status)
        }
        self.pool = created
    }

    deinit {
        __VTMotionEstimationSessionInvalidate(session)
    }

    func analyze(_ image: CIImage) async -> MotionResult {
        guard let buffer = makeAnalysisBuffer(image) else {
            return MotionResult(candidate: false, score: 0, confidence: 0, boundingBox: nil)
        }
        guard let previous else {
            self.previous = buffer
            return MotionResult(candidate: false, score: 0, confidence: 0, boundingBox: nil)
        }
        self.previous = buffer
        return Self.estimate(session: session, previous: previous, current: buffer, roi: roi)
    }

    private func makeAnalysisBuffer(_ image: CIImage) -> CVPixelBuffer? {
        guard let pool else { return nil }
        var output: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(nil, pool, &output) == kCVReturnSuccess,
              let output else { return nil }

        let extent = image.extent
        let scaleX = CGFloat(width) / max(extent.width, 1)
        let scaleY = CGFloat(height) / max(extent.height, 1)
        let normalized = image
            .transformed(by: CGAffineTransform(translationX: -extent.minX, y: -extent.minY))
            .transformed(by: CGAffineTransform(scaleX: scaleX, y: scaleY))
        context.render(
            normalized,
            to: output,
            bounds: CGRect(x: 0, y: 0, width: width, height: height),
            colorSpace: CGColorSpaceCreateDeviceRGB()
        )
        return output
    }

    nonisolated private static func summarize(_ vectors: CVPixelBuffer, roi: [CGPoint]) -> MotionResult {
        let gridWidth = max(CVPixelBufferGetWidth(vectors), 1)
        let gridHeight = max(CVPixelBufferGetHeight(vectors), 1)
        var active = 0
        var eligible = 0
        var minX = gridWidth
        var minY = gridHeight
        var maxX = -1
        var maxY = -1

        CVPixelBufferLockBaseAddress(vectors, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(vectors, .readOnly) }
        let planeIndex = CVPixelBufferIsPlanar(vectors) ? 0 : -1
        let rowBytes = planeIndex >= 0 ? CVPixelBufferGetBytesPerRowOfPlane(vectors, planeIndex) : CVPixelBufferGetBytesPerRow(vectors)
        let base = planeIndex >= 0 ? CVPixelBufferGetBaseAddressOfPlane(vectors, planeIndex) : CVPixelBufferGetBaseAddress(vectors)
        if let base {
            let bytes = base.assumingMemoryBound(to: UInt8.self)
            for y in 0..<gridHeight {
                for x in 0..<gridWidth {
                    let nx = (Double(x) + 0.5) / Double(gridWidth)
                    let ny = (Double(y) + 0.5) / Double(gridHeight)
                    guard pointInROI(x: nx, y: ny, roi: roi) else { continue }
                    eligible += 1
                    let offset = y * rowBytes + x * 4
                    let dx = Int16(bitPattern: UInt16(bytes[offset]) | UInt16(bytes[offset + 1]) << 8)
                    let dy = Int16(bitPattern: UInt16(bytes[offset + 2]) | UInt16(bytes[offset + 3]) << 8)
                    let magnitude = abs(Int(dx)) + abs(Int(dy))
                    if magnitude >= 6 {
                        active += 1
                        minX = min(minX, x)
                        minY = min(minY, y)
                        maxX = max(maxX, x)
                        maxY = max(maxY, y)
                    }
                }
            }
        }

        let score = eligible > 0 ? Double(active) / Double(eligible) : 0
        // A near-global one-frame change is normally exposure or lighting, not an object.
        let candidate = score >= 0.012 && score < 0.65
        let confidence = min(1, max(0, (score - 0.006) / 0.08))
        let box: NormalizedRect? = maxX >= minX && maxY >= minY
            ? NormalizedRect(
                x: Double(minX) / Double(gridWidth),
                y: Double(minY) / Double(gridHeight),
                width: Double(maxX - minX + 1) / Double(gridWidth),
                height: Double(maxY - minY + 1) / Double(gridHeight)
            )
            : nil
        return MotionResult(candidate: candidate, score: score, confidence: confidence, boundingBox: box)
    }

    nonisolated private static func estimate(
        session: __VTMotionEstimationSession,
        previous: CVPixelBuffer,
        current: CVPixelBuffer,
        roi: [CGPoint]
    ) -> MotionResult {
        let box = MotionRequestBox()
        let flags = __VTMotionEstimationFrameFlags(rawValue: 1)
        let status = __VTMotionEstimationSessionEstimateMotionVectors(
            session, previous, current, flags, nil
        ) { status, _, _, vectorBuffer in
            if status == noErr, let vectorBuffer {
                box.lock.withLock {
                    box.result = summarize(vectorBuffer, roi: roi)
                }
            }
            box.semaphore.signal()
        }
        guard status == noErr else { return box.result }
        guard box.semaphore.wait(timeout: .now() + 1) == .success else { return box.result }
        return box.lock.withLock { box.result }
    }

    nonisolated private static func pointInROI(x: Double, y: Double, roi: [CGPoint]) -> Bool {
        var inside = false
        var j = roi.count - 1
        for i in roi.indices {
            let xi = Double(roi[i].x), yi = Double(roi[i].y)
            let xj = Double(roi[j].x), yj = Double(roi[j].y)
            let intersects = ((yi > y) != (yj > y))
                && (x < (xj - xi) * (y - yi) / ((yj - yi) == 0 ? 0.000_001 : (yj - yi)) + xi)
            if intersects { inside.toggle() }
            j = i
        }
        return inside
    }
}

enum MotionError: Error {
    case pixelBufferPool(OSStatus)
    case session(OSStatus)
}
