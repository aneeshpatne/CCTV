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
    private var lastAccepted: ContinuousClock.Instant?
    private var nextOutputFrame = 0
    private var lastOutputBuffer: CVPixelBuffer?
    private var lastHealth = Date.distantPast
    private var fpsWindowStart = Date()
    private var fpsWindowFrames = 0
    private var measuredFPS = 0.0
    private var wasMotionActive = false

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
        let client = MJPEGClient(url: configuration.streamURL)
        client.start()
        let telemetryTask = Task { await telemetry.pollForever() }
        defer {
            telemetryTask.cancel()
            client.stop()
            recorder.finish()
            publisher.stop()
        }

        let clock = ContinuousClock()
        let interval = Duration.seconds(1 / configuration.targetFPS)
        for await jpeg in client.frames {
            let nowMonotonic = clock.now
            if let lastAccepted, nowMonotonic - lastAccepted < interval { continue }
            lastAccepted = nowMonotonic
            if firstMonotonic == nil { firstMonotonic = nowMonotonic }
            guard let source = renderer.decodeJPEG(jpeg) else { continue }

            let wallClock = Date()
            if firstWallClock == nil { firstWallClock = wallClock }
            let motion = await motionDetector.analyze(source)
            let labels = await semanticClassifier.labels(for: source, candidate: motion.candidate, now: wallClock)
            if let event = await accumulator.update(
                candidate: motion.candidate,
                confidence: motion.confidence,
                semanticLabels: labels,
                at: wallClock
            ) {
                emitter.emit(event)
            }
            if motion.candidate && !wasMotionActive {
                blinkCameraLED()
            }
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
            guard let output = renderer.render(source, status: status, now: wallClock),
                  let firstMonotonic else { continue }
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
                        fps: measuredFPS,
                        droppedFrames: client.droppedFrameCount(),
                        motionScore: motion.score,
                        recording: recorder.isRecording,
                        rtsp: publisher.isRunning
                    )
                ))
            }
        }
        throw PipelineError.streamEnded
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

enum PipelineError: Error {
    case streamEnded
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
