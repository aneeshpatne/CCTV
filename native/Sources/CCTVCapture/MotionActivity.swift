import Foundation

struct MotionActivityUpdate: Sendable {
    let active: Bool
    let started: Bool
}

struct MotionActivityGuard: Sendable {
    let holdDuration: TimeInterval
    /// Require this many true candidates inside the rolling window before a new episode.
    let persistenceRequired: Int
    private var recent: [Bool]
    private var recentIndex = 0
    private var activeUntil: TimeInterval?

    init(holdDuration: TimeInterval = 10, persistenceWindow: Int = 3, persistenceRequired: Int = 2) {
        self.holdDuration = holdDuration
        self.persistenceRequired = max(1, min(persistenceRequired, max(1, persistenceWindow)))
        self.recent = [Bool](repeating: false, count: max(1, persistenceWindow))
    }

    mutating func update(candidate: Bool, at now: TimeInterval) -> MotionActivityUpdate {
        recent[recentIndex] = candidate
        recentIndex = (recentIndex + 1) % recent.count
        // Require the current frame plus recent support so single-frame noise is
        // ignored, while a quiet frame never extends the hold from stale history.
        let accepted = candidate && recent.filter(\.self).count >= persistenceRequired

        if accepted {
            let started = activeUntil == nil || now >= activeUntil!
            activeUntil = now + holdDuration
            return MotionActivityUpdate(active: true, started: started)
        }

        guard let activeUntil else {
            return MotionActivityUpdate(active: false, started: false)
        }
        if now < activeUntil {
            return MotionActivityUpdate(active: true, started: false)
        }

        self.activeUntil = nil
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
