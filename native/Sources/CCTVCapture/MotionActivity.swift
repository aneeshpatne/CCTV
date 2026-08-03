import Foundation

struct MotionActivityUpdate: Sendable {
    let active: Bool
    let started: Bool
}

struct MotionActivityGuard: Sendable {
    let holdDuration: TimeInterval
    /// Require this many *consecutive* true candidates before a new episode.
    /// Consecutive (not rolling-count) rejects alternating light-frequency hits.
    let persistenceRequired: Int
    private var consecutiveHits = 0
    private var activeUntil: TimeInterval?

    /// Three consecutive positives ≈ 0.3–0.6s at typical ESP32-CAM FPS; enough to
    /// ignore single-frame noise and every-other-frame AC flicker artifacts.
    init(holdDuration: TimeInterval = 10, persistenceRequired: Int = 3) {
        self.holdDuration = holdDuration
        self.persistenceRequired = max(1, persistenceRequired)
    }

    mutating func update(candidate: Bool, at now: TimeInterval) -> MotionActivityUpdate {
        if candidate {
            consecutiveHits += 1
        } else {
            consecutiveHits = 0
        }

        let episodeActive = activeUntil.map { now < $0 } ?? false
        // Starting a new episode needs consecutive hits. Once live, any candidate
        // extends the quiet-hold so brief detector gaps do not end real motion.
        let accepted = candidate && (episodeActive || consecutiveHits >= persistenceRequired)

        if accepted {
            let started = !episodeActive
            activeUntil = now + holdDuration
            return MotionActivityUpdate(active: true, started: started)
        }

        if episodeActive {
            return MotionActivityUpdate(active: true, started: false)
        }

        // Episode ended: drop the streak so a leftover hit does not immediately re-arm.
        if activeUntil != nil {
            activeUntil = nil
            consecutiveHits = 0
        }
        return MotionActivityUpdate(active: false, started: false)
    }
}

struct LEDBlinkStep: Sendable, Equatable {
    let brightness: Int
    let duration: TimeInterval
}

actor CameraLEDBlinker {
    static let pattern = [
        LEDBlinkStep(brightness: 10, duration: 0.2),
        LEDBlinkStep(brightness: 0, duration: 0.2),
        LEDBlinkStep(brightness: 10, duration: 0.2),
        LEDBlinkStep(brightness: 0, duration: 1.0),
    ]

    private let baseURL: URL
    private let duration: TimeInterval
    private let session: URLSession
    private var blinkTask: Task<Void, Never>?
    private var pendingStart = false

    init(baseURL: URL, duration: TimeInterval = 30) {
        self.baseURL = baseURL
        self.duration = duration
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 1
        configuration.timeoutIntervalForResource = 1
        self.session = URLSession(configuration: configuration)
    }

    func start() {
        guard blinkTask == nil else {
            // FramePipeline calls start only on a new episode. Preserve that edge
            // if the previous sequence is still sending its final LED-off request.
            pendingStart = true
            return
        }
        blinkTask = Task { await runSequence() }
    }

    private func runSequence() async {
        let deadline = ProcessInfo.processInfo.systemUptime + duration

        sequence: while !Task.isCancelled && ProcessInfo.processInfo.systemUptime < deadline {
            for step in Self.pattern {
                guard !Task.isCancelled, ProcessInfo.processInfo.systemUptime < deadline else {
                    break sequence
                }
                await setLED(step.brightness)
                let remaining = deadline - ProcessInfo.processInfo.systemUptime
                guard remaining > 0 else { break sequence }
                let sleep = min(step.duration, remaining)
                try? await Task.sleep(for: .milliseconds(max(1, Int(sleep * 1_000))))
            }
        }

        await setLED(0)
        blinkTask = nil
        if pendingStart {
            pendingStart = false
            start()
        }
    }

    private func setLED(_ brightness: Int) async {
        guard let url = URL(
            string: "/control?var=led_intensity&val=\(brightness)",
            relativeTo: baseURL
        ) else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1
        _ = try? await session.data(for: request)
    }
}
