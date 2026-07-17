@preconcurrency import CoreImage
import CoreMedia
import CoreVideo
import Foundation

private struct SendablePixelBuffer: @unchecked Sendable {
    let value: CVPixelBuffer
}

struct CameraPresentationTimeline: Sendable {
    let originUptime: TimeInterval
    private(set) var lastPresentationTime = CMTime.invalid

    init(originUptime: TimeInterval) {
        self.originUptime = originUptime
    }

    mutating func presentationTime(for monotonicTime: TimeInterval) -> CMTime {
        var pts = CMTime(seconds: max(0, monotonicTime - originUptime), preferredTimescale: 90_000)
        if lastPresentationTime.isValid, pts <= lastPresentationTime {
            pts = lastPresentationTime + CMTime(value: 1, timescale: 90_000)
        }
        lastPresentationTime = pts
        return pts
    }
}

private struct RuntimeSnapshot: Sendable {
    let lastFrameAge: TimeInterval
    let processedFPS: Double
    let processingLatencyMS: Double
    let motionScore: Double
    let sceneBrightness: Double?
    let redOverGreen: Double?
    let blueOverGreen: Double?
}

private actor PipelineRuntimeState {
    private let startedAt = ProcessInfo.processInfo.systemUptime
    private var lastFrameAt: TimeInterval?
    private var processedFrameTimes: [TimeInterval] = []
    private var latencySamplesMS: [Double] = []
    private var motionScore = 0.0
    private var sceneBrightness: Double?
    private var redOverGreen: Double?
    private var blueOverGreen: Double?
    private var reconnectRequested = false

    func recordReceived(at monotonicTime: TimeInterval) {
        if let lastFrameAt, monotonicTime - lastFrameAt >= 3 {
            sceneBrightness = nil
            redOverGreen = nil
            blueOverGreen = nil
        }
        lastFrameAt = monotonicTime
        reconnectRequested = false
    }

    @discardableResult
    func recordProcessed(at monotonicTime: TimeInterval, motionScore: Double) -> Double {
        processedFrameTimes.append(monotonicTime)
        processedFrameTimes.removeAll { monotonicTime - $0 > 5 }
        if processedFrameTimes.count > 120 {
            processedFrameTimes.removeFirst(processedFrameTimes.count - 120)
        }
        self.motionScore = motionScore
        return Self.rate(for: processedFrameTimes)
    }

    func recordLatency(_ milliseconds: Double) {
        latencySamplesMS.append(milliseconds)
        if latencySamplesMS.count > 60 {
            latencySamplesMS.removeFirst(latencySamplesMS.count - 60)
        }
    }

    @discardableResult
    func recordImageMetrics(
        brightness: Double?, redRatio: Double?, blueRatio: Double?
    ) -> (Double?, Double?, Double?) {
        guard let brightness else { return (sceneBrightness, redOverGreen, blueOverGreen) }
        // Smooth frame-level compression noise; the orchestrator applies the longer
        // 30-60 second decision window.
        sceneBrightness = sceneBrightness.map { 0.9 * $0 + 0.1 * brightness } ?? brightness
        if let redRatio {
            redOverGreen = redOverGreen.map { 0.9 * $0 + 0.1 * redRatio } ?? redRatio
        }
        if let blueRatio {
            blueOverGreen = blueOverGreen.map { 0.9 * $0 + 0.1 * blueRatio } ?? blueRatio
        }
        return (sceneBrightness, redOverGreen, blueOverGreen)
    }

    func shouldReconnect(at now: TimeInterval) -> Bool {
        let reference = lastFrameAt ?? startedAt
        guard now - reference >= 3, !reconnectRequested else { return false }
        reconnectRequested = true
        return true
    }

    func snapshot(at now: TimeInterval) -> RuntimeSnapshot {
        processedFrameTimes.removeAll { now - $0 > 5 }
        let reference = lastFrameAt ?? startedAt
        let latency = latencySamplesMS.isEmpty
            ? 0
            : latencySamplesMS.reduce(0, +) / Double(latencySamplesMS.count)
        return RuntimeSnapshot(
            lastFrameAge: now - reference,
            processedFPS: Self.rate(for: processedFrameTimes),
            processingLatencyMS: latency,
            motionScore: motionScore,
            sceneBrightness: sceneBrightness,
            redOverGreen: redOverGreen,
            blueOverGreen: blueOverGreen
        )
    }

    private static func rate(for times: [TimeInterval]) -> Double {
        guard let first = times.first, let last = times.last, last > first else { return 0 }
        return Double(times.count - 1) / (last - first)
    }
}

private struct OutputMetrics: Sendable {
    let outputFPS: Double
    let encoderDroppedFrames: Int
    let recording: Bool
    let rtsp: Bool
}

private actor FrameOutput {
    private let configuration: PipelineConfiguration
    private let recorder: SegmentRecorder
    private let publisher: RTSPPublisher
    private var timeline: CameraPresentationTimeline
    private var h264Encoder: H264HardwareEncoder?
    private var outputFrameTimes: [TimeInterval] = []
    private var encoderDroppedFrames = 0

    init(configuration: PipelineConfiguration, emitter: EventEmitter) throws {
        self.configuration = configuration
        self.recorder = try SegmentRecorder(configuration: configuration, emitter: emitter)
        self.publisher = RTSPPublisher(rtspURL: configuration.rtspURL)
        self.timeline = CameraPresentationTimeline(originUptime: ProcessInfo.processInfo.systemUptime)
    }

    func append(_ boxedBuffer: SendablePixelBuffer, monotonicTime: TimeInterval, wallClock: Date) throws {
        let buffer = boxedBuffer.value
        if h264Encoder == nil {
            h264Encoder = try H264HardwareEncoder(
                width: CVPixelBufferGetWidth(buffer),
                height: CVPixelBufferGetHeight(buffer),
                targetFPS: configuration.targetFPS,
                bitrate: configuration.rtspBitrate,
                publisher: publisher
            )
        }

        let pts = timeline.presentationTime(for: monotonicTime)

        let archiveAccepted = recorder.append(buffer, presentationTime: pts, wallClock: wallClock)
        let rtspAccepted = h264Encoder?.encode(buffer, presentationTime: pts) == true
        if !archiveAccepted || !rtspAccepted { encoderDroppedFrames += 1 }

        outputFrameTimes.append(monotonicTime)
        outputFrameTimes.removeAll { monotonicTime - $0 > 5 }
        if outputFrameTimes.count > 120 {
            outputFrameTimes.removeFirst(outputFrameTimes.count - 120)
        }
    }

    func metrics(at now: TimeInterval) -> OutputMetrics {
        outputFrameTimes.removeAll { now - $0 > 5 }
        let fps: Double
        if let first = outputFrameTimes.first, let last = outputFrameTimes.last, last > first {
            fps = Double(outputFrameTimes.count - 1) / (last - first)
        } else {
            fps = 0
        }
        return OutputMetrics(
            outputFPS: fps,
            encoderDroppedFrames: encoderDroppedFrames,
            recording: recorder.isRecording,
            rtsp: publisher.isRunning
        )
    }

    func finish() {
        recorder.finish()
        publisher.stop()
    }
}

final class FramePipeline: @unchecked Sendable {
    private let configuration: PipelineConfiguration
    private let emitter: EventEmitter
    private let renderer: HUDRenderer
    private let motionDetector: MotionDetector
    private let semanticClassifier = SemanticClassifier()
    private let accumulator = MotionEventAccumulator(cooldown: 15)
    private let ledBlinker: CameraLEDBlinker
    private let telemetry: CameraTelemetry
    private let output: FrameOutput
    private let runtime = PipelineRuntimeState()
    private var motionActivity = MotionActivityGuard(holdDuration: 10)

    init(configuration: PipelineConfiguration) throws {
        self.configuration = configuration
        self.emitter = EventEmitter(fileDescriptor: configuration.eventFileDescriptor)
        self.renderer = HUDRenderer()
        self.motionDetector = try MotionDetector(context: renderer.context)
        self.ledBlinker = CameraLEDBlinker(baseURL: configuration.cameraBaseURL, duration: 10)
        self.telemetry = CameraTelemetry(baseURL: configuration.cameraBaseURL)
        self.output = try FrameOutput(configuration: configuration, emitter: emitter)
    }

    func run() async throws {
        let client = MJPEGClient(url: configuration.streamURL) { [emitter] connected, reason in
            emitter.emit(WorkerEvent(
                type: connected ? "stream.connected" : "stream.disconnected",
                payload: .stream(connected: connected, reason: reason)
            ))
        }
        client.start()
        let frameTask = Task { [weak self] in
            guard let self else { return }
            for await frame in client.frames {
                if Task.isCancelled { break }
                await self.process(frame)
            }
        }
        let telemetryTask = Task { await telemetry.pollForever() }

        var noSignalFrame: CVPixelBuffer?
        var noSignalRenderedAt = Date.distantPast
        var nextKeepaliveAt = ProcessInfo.processInfo.systemUptime
        var lastHealth = Date.distantPast
        var lastImageMetrics = Date.distantPast
        let keepaliveInterval = 1 / configuration.targetFPS

        while !Task.isCancelled {
            let monotonicNow = ProcessInfo.processInfo.systemUptime
            let wallClock = Date()
            let snapshot = await runtime.snapshot(at: monotonicNow)

            if snapshot.lastFrameAge >= 3 {
                if await runtime.shouldReconnect(at: monotonicNow) {
                    client.reconnectStalledStream(reason: "no JPEG received for 3 seconds")
                }
                if noSignalFrame == nil || wallClock.timeIntervalSince(noSignalRenderedAt) >= 1 {
                    let (rssi, temperature, cameraSettings) = await telemetry.snapshot()
                    noSignalFrame = renderer.renderNoSignal(
                        status: HUDStatus(
                            fps: 0,
                            rssi: rssi,
                            temperature: temperature,
                            message: "NO SIGNAL · RECONNECTING",
                            cameraSettings: cameraSettings
                        ),
                        now: wallClock
                    )
                    noSignalRenderedAt = wallClock
                }
                if monotonicNow >= nextKeepaliveAt, let noSignalFrame {
                    try? await output.append(
                        SendablePixelBuffer(value: noSignalFrame),
                        monotonicTime: monotonicNow,
                        wallClock: wallClock
                    )
                    repeat {
                        nextKeepaliveAt += keepaliveInterval
                    } while nextKeepaliveAt <= monotonicNow
                }
            } else {
                noSignalFrame = nil
                nextKeepaliveAt = monotonicNow
            }

            if wallClock.timeIntervalSince(lastHealth) >= 10 {
                lastHealth = wallClock
                let streamMetrics = client.metrics()
                let outputMetrics = await output.metrics(at: monotonicNow)
                let signalAvailable = snapshot.lastFrameAge < 3
                emitter.emit(WorkerEvent(
                    type: "health",
                    payload: .health(
                        fps: signalAvailable ? snapshot.processedFPS : 0,
                        cameraFPS: signalAvailable ? streamMetrics.cameraFPS : 0,
                        outputFPS: outputMetrics.outputFPS,
                        droppedFrames: streamMetrics.droppedFrames,
                        encoderDroppedFrames: outputMetrics.encoderDroppedFrames,
                        processingLatencyMS: snapshot.processingLatencyMS,
                        motionScore: snapshot.motionScore,
                        sceneBrightness: signalAvailable ? snapshot.sceneBrightness : nil,
                        redOverGreen: signalAvailable ? snapshot.redOverGreen : nil,
                        blueOverGreen: signalAvailable ? snapshot.blueOverGreen : nil,
                        recording: outputMetrics.recording,
                        rtsp: outputMetrics.rtsp
                    )
                ))
            }
            if wallClock.timeIntervalSince(lastImageMetrics) >= 2 {
                lastImageMetrics = wallClock
                let signalAvailable = snapshot.lastFrameAge < 3
                emitter.emit(WorkerEvent(
                    type: "image.metrics",
                    payload: .imageMetrics(
                        sceneBrightness: signalAvailable ? snapshot.sceneBrightness : nil,
                        redOverGreen: signalAvailable ? snapshot.redOverGreen : nil,
                        blueOverGreen: signalAvailable ? snapshot.blueOverGreen : nil
                    )
                ))
            }
            let healthDueIn = max(0.005, 10 - wallClock.timeIntervalSince(lastHealth))
            let imageMetricsDueIn = max(0.005, 2 - wallClock.timeIntervalSince(lastImageMetrics))
            let sleepSeconds: TimeInterval
            if snapshot.lastFrameAge >= 3 {
                let keepaliveDueIn = max(0.005, nextKeepaliveAt - monotonicNow)
                let renderDueIn = max(0.005, 1 - wallClock.timeIntervalSince(noSignalRenderedAt))
                sleepSeconds = min(keepaliveDueIn, renderDueIn, healthDueIn, imageMetricsDueIn)
            } else {
                // Frame processing runs independently and does not require polling here.
                // Wake at the next health report or the exact stalled-stream deadline.
                sleepSeconds = min(
                    max(0.005, 3 - snapshot.lastFrameAge),
                    healthDueIn,
                    imageMetricsDueIn
                )
            }
            try? await Task.sleep(for: .milliseconds(Int(sleepSeconds * 1_000)))
        }

        frameTask.cancel()
        telemetryTask.cancel()
        client.stop()
        await output.finish()
    }

    private func process(_ frame: JPEGFrame) async {
        await runtime.recordReceived(at: frame.monotonicTime)
        guard let source = renderer.decodeJPEG(frame.data) else { return }

        let motion = await motionDetector.analyze(source)
        let motionState = motionActivity.update(
            candidate: motion.candidate,
            at: frame.monotonicTime
        )
        if motionState.started {
            await ledBlinker.start()
        }
        let hudMetrics = await runtime.recordImageMetrics(
            brightness: motion.sceneBrightness,
            redRatio: motion.redOverGreen,
            blueRatio: motion.blueOverGreen
        )
        let labels = semanticClassifier.labels(
            for: source,
            candidate: motion.candidate,
            now: frame.receivedAt
        ) { [accumulator] labels in
            Task { await accumulator.merge(semanticLabels: labels) }
        }
        if let event = await accumulator.update(
            candidate: motion.candidate,
            confidence: motion.confidence,
            semanticLabels: labels,
            at: frame.receivedAt
        ) {
            emitter.emit(event)
        }
        let measuredFPS = await runtime.recordProcessed(
            at: ProcessInfo.processInfo.systemUptime,
            motionScore: motion.score
        )
        let (rssi, temperature, cameraSettings) = await telemetry.snapshot()
        let status = HUDStatus(
            fps: measuredFPS,
            rssi: rssi,
            temperature: temperature,
            sceneBrightness: hudMetrics.0,
            redOverGreen: hudMetrics.1,
            blueOverGreen: hudMetrics.2,
            motion: motionState.active,
            labels: labels,
            motionBox: motion.boundingBox,
            message: nil,
            cameraSettings: cameraSettings
        )
        if let rendered = renderer.render(source, status: status, now: frame.receivedAt) {
            try? await output.append(
                SendablePixelBuffer(value: rendered),
                monotonicTime: frame.monotonicTime,
                wallClock: frame.receivedAt
            )
        }
        let latency = max(0, ProcessInfo.processInfo.systemUptime - frame.monotonicTime) * 1_000
        await runtime.recordLatency(latency)
    }

}

Task {
    do {
        let configuration = try PipelineConfiguration.load()
        FileHandle.standardError.write(Data("CCTV native capture starting: \(configuration.streamURL.absoluteString)\n".utf8))
        let pipeline = try FramePipeline(configuration: configuration)
        try await pipeline.run()
    } catch {
        FileHandle.standardError.write(Data("CCTV native capture stopped: \(error)\n".utf8))
        exit(1)
    }
}
dispatchMain()
