import Foundation

struct NormalizedRect: Codable, Sendable, Equatable {
    var x: Double
    var y: Double
    var width: Double
    var height: Double
}

struct SemanticLabel: Codable, Sendable, Equatable {
    let name: String
    let confidence: Double
}

struct WorkerEvent: Encodable, Sendable {
    let version = 1
    let type: String
    let timestamp: String
    let payload: Payload

    enum Payload: Encodable, Sendable {
        case motion(start: Double, end: Double, duration: Double, confidence: Double, labels: [SemanticLabel])
        case segment(path: String, start: Double, end: Double, duration: Double, codec: String, size: Int64)
        case health(fps: Double, droppedFrames: Int, motionScore: Double, recording: Bool, rtsp: Bool)
        case stream(connected: Bool, reason: String?)

        private enum CodingKeys: String, CodingKey {
            case startTime = "start_time"
            case endTime = "end_time"
            case duration, confidence, labels, detectorVersion = "detector_version"
            case path, codec, size, fps, droppedFrames = "dropped_frames", motionScore = "motion_score"
            case recording, rtsp
            case connected, reason
        }

        func encode(to encoder: any Encoder) throws {
            var container = encoder.container(keyedBy: CodingKeys.self)
            switch self {
            case let .motion(start, end, duration, confidence, labels):
                try container.encode(start, forKey: .startTime)
                try container.encode(end, forKey: .endTime)
                try container.encode(duration, forKey: .duration)
                try container.encode(confidence, forKey: .confidence)
                try container.encode(labels, forKey: .labels)
                try container.encode("vt-motion-v1", forKey: .detectorVersion)
            case let .segment(path, start, end, duration, codec, size):
                try container.encode(path, forKey: .path)
                try container.encode(start, forKey: .startTime)
                try container.encode(end, forKey: .endTime)
                try container.encode(duration, forKey: .duration)
                try container.encode(codec, forKey: .codec)
                try container.encode(size, forKey: .size)
            case let .health(fps, dropped, score, recording, rtsp):
                try container.encode(fps, forKey: .fps)
                try container.encode(dropped, forKey: .droppedFrames)
                try container.encode(score, forKey: .motionScore)
                try container.encode(recording, forKey: .recording)
                try container.encode(rtsp, forKey: .rtsp)
            case let .stream(connected, reason):
                try container.encode(connected, forKey: .connected)
                try container.encodeIfPresent(reason, forKey: .reason)
            }
        }
    }

    init(type: String, payload: Payload) {
        self.type = type
        self.payload = payload
        self.timestamp = ISO8601DateFormatter().string(from: Date())
    }
}

final class EventEmitter: @unchecked Sendable {
    private let handle: FileHandle?
    private let encoder = JSONEncoder()
    private let lock = NSLock()

    init(fileDescriptor: Int32?) {
        if let fileDescriptor, fcntl(fileDescriptor, F_GETFD) != -1 {
            self.handle = FileHandle(fileDescriptor: fileDescriptor, closeOnDealloc: false)
        } else {
            self.handle = nil
        }
    }

    func emit(_ event: WorkerEvent) {
        lock.lock()
        defer { lock.unlock() }
        do {
            var data = try encoder.encode(event)
            data.append(0x0A)
            if let handle {
                try handle.write(contentsOf: data)
            } else if let line = String(data: data, encoding: .utf8) {
                print("CCTV_EVENT \(line)", terminator: "")
            }
        } catch {
            FileHandle.standardError.write(Data("event emit failed: \(error)\n".utf8))
        }
    }
}

actor MotionEventAccumulator {
    private let cooldown: TimeInterval
    private var recent = [Bool](repeating: false, count: 3)
    private var recentIndex = 0
    private var eventStart: Date?
    private var lastMotion: Date?
    private var labels: [String: Double] = [:]
    private var peakConfidence = 0.0

    init(cooldown: TimeInterval = 15) {
        self.cooldown = cooldown
    }

    func update(candidate: Bool, confidence: Double, semanticLabels: [SemanticLabel], at now: Date) -> WorkerEvent? {
        recent[recentIndex] = candidate
        recentIndex = (recentIndex + 1) % recent.count
        let accepted = recent.filter { $0 }.count >= 2

        if accepted {
            if eventStart == nil { eventStart = now }
            lastMotion = now
            peakConfidence = max(peakConfidence, confidence)
            for label in semanticLabels {
                labels[label.name] = max(labels[label.name] ?? 0, label.confidence)
            }
            return nil
        }

        guard let start = eventStart, let last = lastMotion, now.timeIntervalSince(last) >= cooldown else {
            return nil
        }

        let paddedStart = start.addingTimeInterval(-cooldown)
        let paddedEnd = last.addingTimeInterval(cooldown)
        let sortedLabels = labels
            .map { SemanticLabel(name: $0.key, confidence: $0.value) }
            .sorted { $0.confidence > $1.confidence }
        let event = WorkerEvent(
            type: "motion.finalized",
            payload: .motion(
                start: paddedStart.timeIntervalSince1970,
                end: paddedEnd.timeIntervalSince1970,
                duration: paddedEnd.timeIntervalSince(paddedStart),
                confidence: peakConfidence,
                labels: sortedLabels
            )
        )
        eventStart = nil
        lastMotion = nil
        labels.removeAll(keepingCapacity: true)
        peakConfidence = 0
        return event
    }
}
