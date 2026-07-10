@preconcurrency import CoreImage
import CoreMedia
import CoreVideo
import Foundation

final class FramePipeline: @unchecked Sendable {
    private let configuration: PipelineConfiguration
    private let emitter: EventEmitter
    private let renderer: HUDRenderer
    private let motionDetector: MotionDetector
    private let semanticClassifier = SemanticClassifier()
    private let accumulator = MotionEventAccumulator(cooldown: 15)
    private let telemetry: CameraTelemetry
    private let recorder: SegmentRecorder
    private let publisher: RTSPPublisher
    private var h264Encoder: H264HardwareEncoder?
    private var firstMonotonic: ContinuousClock.Instant?
    private var firstWallClock: Date?
    private var nextOutputFrame = 0
    private var lastOutputBuffer: CVPixelBuffer?
    private var lastHealth = Date.distantPast
    private var fpsWindowStart = Date()
    private var fpsWindowFrames = 0
    private var measuredFPS = 0.0
    private var wasMotionActive = false
    private var lastMotionScore = 0.0

    init(configuration: PipelineConfiguration) throws {
        self.configuration = configuration
        self.emitter = EventEmitter(fileDescriptor: configuration.eventFileDescriptor)
        self.renderer = HUDRenderer()
        self.motionDetector = try MotionDetector(context: renderer.context)
        self.telemetry = CameraTelemetry(baseURL: configuration.cameraBaseURL)
        self.recorder = try SegmentRecorder(configuration: configuration, emitter: emitter)
        self.publisher = RTSPPublisher(rtspURL: configuration.rtspURL, targetFPS: configuration.targetFPS)
    }

    func run() async throws {
        let mailbox = LatestJPEGMailbox()
        let client = MJPEGClient(url: configuration.streamURL) { [emitter] connected, reason in
            emitter.emit(WorkerEvent(
                type: connected ? "stream.connected" : "stream.disconnected",
                payload: .stream(connected: connected, reason: reason)
            ))
        }
        client.start()
        let frameTask = Task {
            for await jpeg in client.frames { mailbox.put(jpeg) }
        }
        let telemetryTask = Task { await telemetry.pollForever() }
        defer {
            frameTask.cancel()
            telemetryTask.cancel()
            client.stop()
            recorder.finish()
            publisher.stop()
        }

        let clock = ContinuousClock()
        let interval = Duration.seconds(1 / configuration.targetFPS)
        firstMonotonic = clock.now
        firstWallClock = Date()
        var sequence = -1
        var lastFrameAt = Date()
        var staleReconnectRequested = false
        var noSignalFrame: CVPixelBuffer?
        var noSignalRenderedAt = Date.distantPast

        while !Task.isCancelled {
            let nowMonotonic = clock.now
            let wallClock = Date()
            if let packet = mailbox.take(after: sequence) {
                sequence = packet.sequence
                lastFrameAt = packet.receivedAt
                staleReconnectRequested = false
                noSignalFrame = nil
                if let source = renderer.decodeJPEG(packet.data) {
                    let motion = await motionDetector.analyze(source)
                    lastMotionScore = motion.score
                    let labels = await semanticClassifier.labels(for: source, candidate: motion.candidate, now: wallClock)
                    if let event = await accumulator.update(
                        candidate: motion.candidate,
                        confidence: motion.confidence,
                        semanticLabels: labels,
                        at: wallClock
                    ) {
                        emitter.emit(event)
                    }
                    if motion.candidate && !wasMotionActive { blinkCameraLED() }
                    wasMotionActive = motion.candidate
                    updateFPS(at: wallClock)
                    let (rssi, temperature) = await telemetry.snapshot()
                    let status = HUDStatus(
                        fps: measuredFPS,
                        rssi: rssi,
                        temperature: temperature,
                        motion: motion.candidate,
                        labels: labels,
                        motionBox: motion.boundingBox,
                        message: nil
                    )
                    if let output = renderer.render(source, status: status, now: wallClock) {
                        lastOutputBuffer = output
                    }
                }
            }

            let frameAge = wallClock.timeIntervalSince(lastFrameAt)
            if sequence < 0 || frameAge >= 3 {
                if frameAge >= 3 && !staleReconnectRequested {
                    staleReconnectRequested = true
                    client.reconnectStalledStream(reason: "no JPEG received for 3 seconds")
                }
                if noSignalFrame == nil || wallClock.timeIntervalSince(noSignalRenderedAt) >= 1 {
                    let (rssi, temperature) = await telemetry.snapshot()
                    noSignalFrame = renderer.renderNoSignal(
                        status: HUDStatus(fps: 0, rssi: rssi, temperature: temperature, message: "NO SIGNAL · RECONNECTING"),
                        now: wallClock
                    )
                    noSignalRenderedAt = wallClock
                }
                if let noSignalFrame { lastOutputBuffer = noSignalFrame }
            }

            guard let output = lastOutputBuffer, let firstMonotonic else {
                try? await Task.sleep(for: interval)
                continue
            }
            let elapsed = firstMonotonic.duration(to: nowMonotonic).seconds
            if h264Encoder == nil {
                h264Encoder = try H264HardwareEncoder(
                    width: CVPixelBufferGetWidth(output),
                    height: CVPixelBufferGetHeight(output),
                    targetFPS: configuration.targetFPS,
                    bitrate: configuration.rtspBitrate,
                    publisher: publisher
                )
            }
            writeAtFixedCadence(output, through: elapsed)

            if wallClock.timeIntervalSince(lastHealth) >= 10 {
                lastHealth = wallClock
                emitter.emit(WorkerEvent(
                    type: "health",
                    payload: .health(
                        fps: frameAge >= 3 ? 0 : measuredFPS,
                        droppedFrames: client.droppedFrameCount(),
                        motionScore: lastMotionScore,
                        recording: recorder.isRecording,
                        rtsp: publisher.isRunning
                    )
                ))
            }
            try? await Task.sleep(for: interval)
        }
    }

    private func writeAtFixedCadence(_ newest: CVPixelBuffer, through elapsed: Double) {
        guard let firstWallClock else { return }
        let fps = configuration.targetFPS
        let targetFrame = Int(floor(elapsed * fps))
        guard targetFrame >= nextOutputFrame else {
            lastOutputBuffer = newest
            return
        }
        let earliestFrame = max(nextOutputFrame, targetFrame - 30)
        if earliestFrame > nextOutputFrame { nextOutputFrame = earliestFrame }
        while nextOutputFrame <= targetFrame {
            let isNewestSlot = nextOutputFrame == targetFrame
            let buffer = isNewestSlot ? newest : (lastOutputBuffer ?? newest)
            let pts = CMTime(value: CMTimeValue(nextOutputFrame), timescale: CMTimeScale(fps.rounded()))
            let wallClock = firstWallClock.addingTimeInterval(Double(nextOutputFrame) / fps)
            recorder.append(buffer, presentationTime: pts, wallClock: wallClock)
            h264Encoder?.encode(buffer, presentationTime: pts)
            nextOutputFrame += 1
        }
        lastOutputBuffer = newest
    }

    private func updateFPS(at now: Date) {
        fpsWindowFrames += 1
        let elapsed = now.timeIntervalSince(fpsWindowStart)
        if elapsed >= 1 {
            measuredFPS = Double(fpsWindowFrames) / elapsed
            fpsWindowFrames = 0
            fpsWindowStart = now
        }
    }

    private func blinkCameraLED() {
        let base = configuration.cameraBaseURL
        Task.detached(priority: .utility) {
            for value in [10, 0] {
                guard let url = URL(string: "/control?var=led_intensity&val=\(value)", relativeTo: base) else { continue }
                _ = try? await URLSession.shared.data(from: url)
                try? await Task.sleep(for: .milliseconds(500))
            }
        }
    }
}

private extension Duration {
    var seconds: Double {
        let components = self.components
        return Double(components.seconds) + Double(components.attoseconds) / 1e18
    }
}

private struct JPEGPacket: Sendable {
    let sequence: Int
    let receivedAt: Date
    let data: Data
}

private final class LatestJPEGMailbox: @unchecked Sendable {
    private let lock = NSLock()
    private var sequence = 0
    private var latest: JPEGPacket?

    func put(_ data: Data) {
        lock.withLock {
            sequence += 1
            latest = JPEGPacket(sequence: sequence, receivedAt: Date(), data: data)
        }
    }

    func take(after previousSequence: Int) -> JPEGPacket? {
        lock.withLock {
            guard let latest, latest.sequence > previousSequence else { return nil }
            return latest
        }
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
