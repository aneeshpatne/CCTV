@preconcurrency import CoreImage
import CoreVideo
import Foundation
@preconcurrency import VideoToolbox

struct MotionResult: Sendable {
    let candidate: Bool
    let score: Double
    let confidence: Double
    let boundingBox: NormalizedRect?
    let sceneBrightness: Double?
    let redOverGreen: Double?
    let blueOverGreen: Double?
}

struct SceneColorMetrics: Sendable, Equatable {
    let brightness: Double
    let redOverGreen: Double?
    let blueOverGreen: Double?
}

private struct MotionPixelBuffer: @unchecked Sendable {
    let value: CVPixelBuffer
}

private final class MotionRequestBox: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<MotionResult, Never>?

    init(_ continuation: CheckedContinuation<MotionResult, Never>) {
        self.continuation = continuation
    }

    func finish(_ result: MotionResult) {
        let pending = lock.withLock { () -> CheckedContinuation<MotionResult, Never>? in
            let value = continuation
            continuation = nil
            return value
        }
        pending?.resume(returning: result)
    }
}

actor MotionDetector {
    private let width = 512
    private let height = 384
    private let context: CIContext
    private let session: __VTMotionEstimationSession
    private let colorSpace = CGColorSpaceCreateDeviceRGB()
    private let roiMask: [Bool]
    private let analysisBuffers: [CVPixelBuffer]
    private var nextBufferIndex = 0
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
        self.roiMask = Self.makeROIMask(width: width / 16, height: height / 16, roi: roi)
        let attributes: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey: width,
            kCVPixelBufferHeightKey: height,
            kCVPixelBufferMetalCompatibilityKey: true,
            kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary,
        ]
        var buffers: [CVPixelBuffer] = []
        for _ in 0..<2 {
            var created: CVPixelBuffer?
            let status = CVPixelBufferCreate(
                nil,
                width,
                height,
                kCVPixelFormatType_32BGRA,
                attributes as CFDictionary,
                &created
            )
            guard status == kCVReturnSuccess, let created else {
                throw MotionError.pixelBufferPool(status)
            }
            buffers.append(created)
        }
        self.analysisBuffers = buffers
    }

    deinit {
        __VTMotionEstimationSessionInvalidate(session)
    }

    func analyze(_ image: CIImage) async -> MotionResult {
        guard let buffer = makeAnalysisBuffer(image) else {
            return MotionResult(
                candidate: false,
                score: 0,
                confidence: 0,
                boundingBox: nil,
                sceneBrightness: nil,
                redOverGreen: nil,
                blueOverGreen: nil
            )
        }
        let metrics = Self.imageMetrics(buffer)
        guard let previous else {
            self.previous = buffer
            return MotionResult(
                candidate: false,
                score: 0,
                confidence: 0,
                boundingBox: nil,
                sceneBrightness: metrics?.brightness,
                redOverGreen: metrics?.redOverGreen,
                blueOverGreen: metrics?.blueOverGreen
            )
        }
        self.previous = buffer
        let motion = await Self.estimate(
            session: session,
            previous: MotionPixelBuffer(value: previous),
            current: MotionPixelBuffer(value: buffer),
            roi: roi,
            roiMask: roiMask
        )
        return MotionResult(
            candidate: motion.candidate,
            score: motion.score,
            confidence: motion.confidence,
            boundingBox: motion.boundingBox,
            sceneBrightness: metrics?.brightness,
            redOverGreen: metrics?.redOverGreen,
            blueOverGreen: metrics?.blueOverGreen
        )
    }

    private func makeAnalysisBuffer(_ image: CIImage) -> CVPixelBuffer? {
        let output = analysisBuffers[nextBufferIndex]
        nextBufferIndex = (nextBufferIndex + 1) % analysisBuffers.count

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
            colorSpace: colorSpace
        )
        return output
    }

    nonisolated private static func summarize(
        _ vectors: CVPixelBuffer,
        roi: [CGPoint],
        roiMask: [Bool]
    ) -> MotionResult {
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
                    let maskIndex = y * gridWidth + x
                    let inROI = roiMask.count == gridWidth * gridHeight
                        ? roiMask[maskIndex]
                        : pointInROI(x: nx, y: ny, roi: roi)
                    guard inROI else { continue }
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
        return MotionResult(
            candidate: candidate,
            score: score,
            confidence: confidence,
            boundingBox: box,
            sceneBrightness: nil,
            redOverGreen: nil,
            blueOverGreen: nil
        )
    }

    /// Estimate BT.709 luma before the HUD is drawn, excluding clipped shadows and
    /// highlights so unavoidable clipping does not dominate exposure correction.
    nonisolated static func imageMetrics(_ buffer: CVPixelBuffer) -> SceneColorMetrics? {
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else { return nil }

        let width = CVPixelBufferGetWidth(buffer)
        let height = CVPixelBufferGetHeight(buffer)
        let rowBytes = CVPixelBufferGetBytesPerRow(buffer)
        let bytes = base.assumingMemoryBound(to: UInt8.self)
        let sampleStep = 4
        var usableLuminance = 0.0
        var usableCount = 0
        var allLuminance = 0.0
        var allCount = 0
        var referenceRedRatios: [Double] = []
        var referenceBlueRatios: [Double] = []

        for y in stride(from: 0, to: height, by: sampleStep) {
            for x in stride(from: 0, to: width, by: sampleStep) {
                let offset = y * rowBytes + x * 4
                let blue = Double(bytes[offset])
                let green = Double(bytes[offset + 1])
                let red = Double(bytes[offset + 2])
                let value = 0.2126 * red + 0.7152 * green + 0.0722 * blue
                allLuminance += value
                allCount += 1
                if value > 7 && value < 248 {
                    usableLuminance += value
                    usableCount += 1
                    let normalizedX = (Double(x) + 0.5) / Double(width)
                    let normalizedY = (Double(y) + 0.5) / Double(height)
                    // The fixed stairwell view contains a painted neutral wall in
                    // this region. Measure its actual cast even when it is strongly
                    // blue or red; selecting only pixels that already look neutral
                    // hid the overnight lighting transitions from the controller.
                    if green > 7,
                       normalizedX >= 0.51, normalizedX < 0.70,
                       normalizedY >= 0.26, normalizedY < 0.74 {
                        referenceRedRatios.append(red / green)
                        referenceBlueRatios.append(blue / green)
                    }
                }
            }
        }
        guard allCount > 0 else { return nil }
        let brightness = usableCount > 0
            ? usableLuminance / (Double(usableCount) * 255)
            : allLuminance / (Double(allCount) * 255)
        let minimumReference = max(1, allCount / 100)
        guard referenceRedRatios.count >= minimumReference else {
            return SceneColorMetrics(brightness: brightness, redOverGreen: nil, blueOverGreen: nil)
        }
        referenceRedRatios.sort()
        referenceBlueRatios.sort()
        let midpoint = referenceRedRatios.count / 2
        return SceneColorMetrics(
            brightness: brightness,
            redOverGreen: referenceRedRatios[midpoint],
            blueOverGreen: referenceBlueRatios[midpoint]
        )
    }

    nonisolated private static func estimate(
        session: __VTMotionEstimationSession,
        previous: MotionPixelBuffer,
        current: MotionPixelBuffer,
        roi: [CGPoint],
        roiMask: [Bool]
    ) async -> MotionResult {
        let empty = MotionResult(
            candidate: false,
            score: 0,
            confidence: 0,
            boundingBox: nil,
            sceneBrightness: nil,
            redOverGreen: nil,
            blueOverGreen: nil
        )
        return await withCheckedContinuation { continuation in
            let box = MotionRequestBox(continuation)
            // Keep ownership explicit with our two persistent buffers. The reuse hint lets
            // VideoToolbox cache a caller-owned buffer and has caused intermittent CVBufferRetain
            // traps on macOS 26 under sustained load.
            let flags = __VTMotionEstimationFrameFlags(rawValue: 0)
            let status = __VTMotionEstimationSessionEstimateMotionVectors(
                session, previous.value, current.value, flags, nil
            ) { status, _, _, vectorBuffer in
                if status == noErr, let vectorBuffer {
                    box.finish(summarize(vectorBuffer, roi: roi, roiMask: roiMask))
                } else {
                    box.finish(empty)
                }
            }
            guard status == noErr else {
                box.finish(empty)
                return
            }
        }
    }

    nonisolated private static func makeROIMask(width: Int, height: Int, roi: [CGPoint]) -> [Bool] {
        guard width > 0, height > 0 else { return [] }
        return (0..<(width * height)).map { index in
            let x = index % width
            let y = index / width
            return pointInROI(
                x: (Double(x) + 0.5) / Double(width),
                y: (Double(y) + 0.5) / Double(height),
                roi: roi
            )
        }
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
