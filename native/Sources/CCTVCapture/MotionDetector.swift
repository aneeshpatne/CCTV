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

/// Single-frame motion gates used by VideoToolbox vector summarization.
/// Tuned to suppress ESP32-CAM sensor noise, JPEG mosquito noise, and AC-mains
/// light flicker (50/60 Hz → rolling bands + global luma pulse) while still
/// accepting compact object-sized motion.
enum MotionScoring {
    /// Stronger blur damps high-frequency JPEG mosquito noise and thin flicker edges.
    static let analysisBlurSigma: Double = 2.0
    /// L1 |dx|+|dy| floor for a 16×16 block. Noise/flicker vectors cluster lower.
    static let minVectorMagnitude = 14
    /// Minimum ROI fraction of active blocks.
    static let minActiveFraction = 0.024
    /// Near-global change is exposure/lighting, not an object.
    static let maxActiveFraction = 0.42
    /// Largest 4-connected active cluster must cover at least this many blocks.
    static let minClusterBlocks = 8
    /// Active fill density inside the largest-cluster bounding box.
    static let minClusterDensity = 0.32
    /// Mean magnitude inside the largest cluster; barely-threshold noise is cooler.
    static let minMeanClusterMagnitude = 18.0
    /// Thin ribbons across most of a frame axis are typical of flicker / rolling bands.
    static let maxRibbonAspect = 6.0
    static let maxRibbonThickness = 3
    /// Wide multi-row bands (rolling-shutter AC flicker) even when not razor-thin.
    static let maxBandWidthFraction = 0.55
    static let maxBandHeightBlocks = 4
    /// Real objects move coherently; light-frequency artifacts scatter vector angles.
    static let minDirectionCoherence = 0.45
    /// Cosine threshold (~55°) for counting a vector as agreeing with the cluster mean.
    static let directionCosineThreshold = 0.55
    /// Clamp for per-frame mean-luma matching that cancels global AC brightness pulse.
    static let lumaMatchScaleMin = 0.78
    static let lumaMatchScaleMax = 1.28
}

private struct MotionActiveCell: Sendable {
    let x: Int
    let y: Int
    let dx: Int
    let dy: Int

    var magnitude: Int { abs(dx) + abs(dy) }
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
    /// Running mean luma of the last analysis frame (after match). Used to cancel
    /// frame-to-frame global brightness pulse from AC-powered lights before ME.
    private var previousMeanLuma: Double?

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
        // Metrics from the raw analysis frame (pre mean-match) so exposure/WB still
        // see true scene brightness rather than the flicker-normalized copy.
        let metrics = Self.imageMetrics(buffer)
        let rawMean = Self.meanLuma(buffer)
        // Cancel global AC light pulse: scale current toward previous mean so pure
        // full-frame brightness oscillation does not seed motion vectors. Local
        // object motion remains after the global gain is removed.
        if let previousMean = previousMeanLuma, let rawMean, rawMean > 1 {
            let scale = min(
                MotionScoring.lumaMatchScaleMax,
                max(MotionScoring.lumaMatchScaleMin, previousMean / rawMean)
            )
            if abs(scale - 1) > 0.008 {
                Self.scaleLuma(buffer, by: scale)
            }
        }
        previousMeanLuma = Self.meanLuma(buffer) ?? rawMean
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
        let analysisBounds = CGRect(x: 0, y: 0, width: width, height: height)
        // Downscale first, then lightly blur so sensor/JPEG noise does not seed
        // spurious motion vectors while real object edges remain usable.
        let normalized = image
            .transformed(by: CGAffineTransform(translationX: -extent.minX, y: -extent.minY))
            .transformed(by: CGAffineTransform(scaleX: scaleX, y: scaleY))
            .clampedToExtent()
            .applyingGaussianBlur(sigma: MotionScoring.analysisBlurSigma)
            .cropped(to: analysisBounds)
        context.render(
            normalized,
            to: output,
            bounds: analysisBounds,
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
        var eligible = 0
        var activeCells: [MotionActiveCell] = []

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
                    let dx = Int(Int16(bitPattern: UInt16(bytes[offset]) | UInt16(bytes[offset + 1]) << 8))
                    let dy = Int(Int16(bitPattern: UInt16(bytes[offset + 2]) | UInt16(bytes[offset + 3]) << 8))
                    let magnitude = abs(dx) + abs(dy)
                    if magnitude >= MotionScoring.minVectorMagnitude {
                        activeCells.append(MotionActiveCell(x: x, y: y, dx: dx, dy: dy))
                    }
                }
            }
        }

        return evaluate(
            activeCells: activeCells,
            gridWidth: gridWidth,
            gridHeight: gridHeight,
            eligible: eligible
        )
    }

    /// Pure scoring path for production summarization and unit tests.
    /// When only magnitude is supplied, vectors are treated as coherent (dx=magnitude, dy=0)
    /// so geometry gates can be tested independently of direction scatter.
    nonisolated static func evaluate(
        activeCells: [(x: Int, y: Int, magnitude: Int)],
        gridWidth: Int,
        gridHeight: Int,
        eligible: Int
    ) -> MotionResult {
        let cells = activeCells.map {
            MotionActiveCell(x: $0.x, y: $0.y, dx: $0.magnitude, dy: 0)
        }
        return evaluate(activeCells: cells, gridWidth: gridWidth, gridHeight: gridHeight, eligible: eligible)
    }

    /// Vector-aware scoring path (preferred for flicker-coherence tests).
    nonisolated static func evaluate(
        activeCells: [(x: Int, y: Int, dx: Int, dy: Int)],
        gridWidth: Int,
        gridHeight: Int,
        eligible: Int
    ) -> MotionResult {
        let cells = activeCells.map {
            MotionActiveCell(x: $0.x, y: $0.y, dx: $0.dx, dy: $0.dy)
        }
        return evaluate(activeCells: cells, gridWidth: gridWidth, gridHeight: gridHeight, eligible: eligible)
    }

    nonisolated private static func evaluate(
        activeCells: [MotionActiveCell],
        gridWidth: Int,
        gridHeight: Int,
        eligible: Int
    ) -> MotionResult {
        let active = activeCells.count
        let score = eligible > 0 ? Double(active) / Double(eligible) : 0
        let confidence = min(1, max(0, (score - 0.012) / 0.08))

        guard
            score >= MotionScoring.minActiveFraction,
            score < MotionScoring.maxActiveFraction,
            let cluster = largestCluster(activeCells, gridWidth: gridWidth),
            cluster.cells.count >= MotionScoring.minClusterBlocks
        else {
            return MotionResult(
                candidate: false,
                score: score,
                confidence: confidence,
                boundingBox: nil,
                sceneBrightness: nil,
                redOverGreen: nil,
                blueOverGreen: nil
            )
        }

        let boxWidth = cluster.maxX - cluster.minX + 1
        let boxHeight = cluster.maxY - cluster.minY + 1
        let boxArea = max(1, boxWidth * boxHeight)
        let density = Double(cluster.cells.count) / Double(boxArea)
        let meanMagnitude = Double(cluster.cells.reduce(0) { $0 + $1.magnitude })
            / Double(cluster.cells.count)
        let aspect = Double(max(boxWidth, boxHeight)) / Double(max(1, min(boxWidth, boxHeight)))
        let ribbonLike = aspect >= MotionScoring.maxRibbonAspect
            && min(boxWidth, boxHeight) <= MotionScoring.maxRibbonThickness
        // Rolling-shutter AC flicker often paints a wide multi-row band rather than
        // a one-pixel ribbon. Reject any short, near-full-width horizontal strip.
        let horizontalBand = Double(boxWidth) / Double(max(1, gridWidth)) >= MotionScoring.maxBandWidthFraction
            && boxHeight <= MotionScoring.maxBandHeightBlocks
        let verticalBand = Double(boxHeight) / Double(max(1, gridHeight)) >= MotionScoring.maxBandWidthFraction
            && boxWidth <= MotionScoring.maxBandHeightBlocks
        let coherence = directionCoherence(cluster.cells)

        let candidate = density >= MotionScoring.minClusterDensity
            && meanMagnitude >= MotionScoring.minMeanClusterMagnitude
            && !ribbonLike
            && !horizontalBand
            && !verticalBand
            && coherence >= MotionScoring.minDirectionCoherence

        let box = NormalizedRect(
            x: Double(cluster.minX) / Double(gridWidth),
            y: Double(cluster.minY) / Double(gridHeight),
            width: Double(boxWidth) / Double(gridWidth),
            height: Double(boxHeight) / Double(gridHeight)
        )
        return MotionResult(
            candidate: candidate,
            score: score,
            confidence: confidence,
            boundingBox: candidate ? box : nil,
            sceneBrightness: nil,
            redOverGreen: nil,
            blueOverGreen: nil
        )
    }

    /// Fraction of cluster cells whose direction agrees with the magnitude-weighted mean.
    /// Flicker / noise scatters; a walking person or animal does not.
    nonisolated static func directionCoherence(_ cells: [(dx: Int, dy: Int)]) -> Double {
        directionCoherence(cells.map { MotionActiveCell(x: 0, y: 0, dx: $0.dx, dy: $0.dy) })
    }

    nonisolated private static func directionCoherence(_ cells: [MotionActiveCell]) -> Double {
        guard !cells.isEmpty else { return 0 }
        var sumX = 0.0
        var sumY = 0.0
        var weightSum = 0.0
        for cell in cells {
            let weight = Double(max(1, cell.magnitude))
            sumX += Double(cell.dx) * weight
            sumY += Double(cell.dy) * weight
            weightSum += weight
        }
        guard weightSum > 0 else { return 0 }
        let meanLength = (sumX * sumX + sumY * sumY).squareRoot()
        // Near-zero mean direction with non-trivial magnitudes ⇒ opposing/random vectors.
        if meanLength < 1e-6 {
            return 0
        }
        let meanX = sumX / meanLength
        let meanY = sumY / meanLength
        var agreeing = 0
        for cell in cells {
            let length = (Double(cell.dx * cell.dx + cell.dy * cell.dy)).squareRoot()
            guard length > 1e-6 else { continue }
            let cosine = (Double(cell.dx) * meanX + Double(cell.dy) * meanY) / length
            if cosine >= MotionScoring.directionCosineThreshold {
                agreeing += 1
            }
        }
        return Double(agreeing) / Double(cells.count)
    }

    nonisolated static func meanLuma(_ buffer: CVPixelBuffer) -> Double? {
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else { return nil }

        let width = CVPixelBufferGetWidth(buffer)
        let height = CVPixelBufferGetHeight(buffer)
        let rowBytes = CVPixelBufferGetBytesPerRow(buffer)
        let bytes = base.assumingMemoryBound(to: UInt8.self)
        let step = 4
        var total = 0.0
        var count = 0
        for y in stride(from: 0, to: height, by: step) {
            for x in stride(from: 0, to: width, by: step) {
                let offset = y * rowBytes + x * 4
                let blue = Double(bytes[offset])
                let green = Double(bytes[offset + 1])
                let red = Double(bytes[offset + 2])
                total += 0.2126 * red + 0.7152 * green + 0.0722 * blue
                count += 1
            }
        }
        guard count > 0 else { return nil }
        return total / Double(count)
    }

    /// In-place RGB scale (alpha untouched) used for global luma matching.
    nonisolated static func scaleLuma(_ buffer: CVPixelBuffer, by scale: Double) {
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else { return }

        let width = CVPixelBufferGetWidth(buffer)
        let height = CVPixelBufferGetHeight(buffer)
        let rowBytes = CVPixelBufferGetBytesPerRow(buffer)
        let bytes = base.assumingMemoryBound(to: UInt8.self)
        for y in 0..<height {
            let row = y * rowBytes
            for x in 0..<width {
                let offset = row + x * 4
                bytes[offset] = UInt8(min(255, max(0, Double(bytes[offset]) * scale)))
                bytes[offset + 1] = UInt8(min(255, max(0, Double(bytes[offset + 1]) * scale)))
                bytes[offset + 2] = UInt8(min(255, max(0, Double(bytes[offset + 2]) * scale)))
            }
        }
    }

    nonisolated private static func largestCluster(
        _ cells: [MotionActiveCell],
        gridWidth: Int
    ) -> (cells: [MotionActiveCell], minX: Int, minY: Int, maxX: Int, maxY: Int)? {
        guard !cells.isEmpty else { return nil }

        var byKey: [Int: MotionActiveCell] = [:]
        byKey.reserveCapacity(cells.count)
        for cell in cells {
            byKey[cell.y * gridWidth + cell.x] = cell
        }

        var visited = Set<Int>()
        visited.reserveCapacity(cells.count)
        var best: [MotionActiveCell] = []

        for cell in cells {
            let startKey = cell.y * gridWidth + cell.x
            guard visited.insert(startKey).inserted else { continue }

            var component: [MotionActiveCell] = []
            var queue: [MotionActiveCell] = [cell]
            var head = 0
            while head < queue.count {
                let current = queue[head]
                head += 1
                component.append(current)
                let neighbors = [
                    (current.x + 1, current.y),
                    (current.x - 1, current.y),
                    (current.x, current.y + 1),
                    (current.x, current.y - 1),
                ]
                for (nx, ny) in neighbors {
                    let key = ny * gridWidth + nx
                    guard let next = byKey[key], visited.insert(key).inserted else { continue }
                    queue.append(next)
                }
            }
            if component.count > best.count {
                best = component
            }
        }

        guard !best.isEmpty else { return nil }
        let minX = best.map(\.x).min()!
        let maxX = best.map(\.x).max()!
        let minY = best.map(\.y).min()!
        let maxY = best.map(\.y).max()!
        return (best, minX, minY, maxX, maxY)
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
