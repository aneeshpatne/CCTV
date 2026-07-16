import Foundation

struct JPEGFrame: Sendable {
    let data: Data
    let receivedAt: Date
    let monotonicTime: TimeInterval
}

struct MJPEGMetrics: Sendable {
    let cameraFPS: Double
    let parsedFrames: Int
    let droppedFrames: Int
}

struct MultipartJPEGParser: Sendable {
    private static let headerTerminator = Data([0x0D, 0x0A, 0x0D, 0x0A])
    private static let startMarker = Data([0xFF, 0xD8])
    private static let endMarker = Data([0xFF, 0xD9])
    private static let maximumBufferSize = 8 * 1024 * 1024
    private static let compactionThreshold = 1024 * 1024

    private var buffer = Data()
    private var readOffset = 0
    private var endMarkerSearchOffset = 0
    private var expectedJPEGLength: Int?

    mutating func reset() {
        buffer.removeAll(keepingCapacity: true)
        readOffset = 0
        endMarkerSearchOffset = 0
        expectedJPEGLength = nil
    }

    /// Parse multipart MJPEG incrementally. Content-Length avoids rescanning JPEG payloads;
    /// SOI/EOI remains a compatibility fallback for cameras with incomplete headers.
    mutating func append(_ data: Data) -> [Data] {
        if buffer.isEmpty {
            buffer.reserveCapacity(max(256 * 1024, data.count))
        }
        buffer.append(data)
        var result: [Data] = []
        while true {
            if let expectedJPEGLength {
                guard unreadCount >= expectedJPEGLength else { break }
                let frameEnd = readOffset + expectedJPEGLength
                let candidate = Data(buffer[readOffset..<frameEnd])
                consume(through: frameEnd)
                self.expectedJPEGLength = nil
                if let jpeg = Self.validJPEG(from: candidate) { result.append(jpeg) }
                continue
            }

            if unreadData.starts(with: Self.startMarker) {
                // Resume at the last unsearched byte. Keeping one trailing byte lets an
                // FF D9 marker split across URLSession callbacks be detected.
                let searchStart = max(readOffset + Self.startMarker.count, endMarkerSearchOffset)
                guard let end = buffer.range(of: Self.endMarker, in: searchStart..<buffer.endIndex) else {
                    endMarkerSearchOffset = max(readOffset + Self.startMarker.count, buffer.endIndex - 1)
                    break
                }
                let frameEnd = end.upperBound
                result.append(Data(buffer[readOffset..<frameEnd]))
                consume(through: frameEnd)
                continue
            }

            if let headerEnd = buffer.range(
                of: Self.headerTerminator,
                in: readOffset..<buffer.endIndex
            ) {
                let headerData = Data(buffer[readOffset..<headerEnd.lowerBound])
                consume(through: headerEnd.upperBound)
                if let length = Self.contentLength(from: headerData),
                   length > 0,
                   length <= Self.maximumBufferSize {
                    expectedJPEGLength = length
                }
                continue
            }

            if let start = buffer.range(
                of: Self.startMarker,
                in: readOffset..<buffer.endIndex
            ), start.lowerBound > readOffset {
                consume(through: start.lowerBound)
                continue
            }

            if unreadCount > Self.maximumBufferSize {
                readOffset = max(readOffset, buffer.endIndex - 2 * 1024 * 1024)
                compact(force: true)
                expectedJPEGLength = nil
            }
            break
        }
        compact(force: false)
        return result
    }

    private var unreadCount: Int {
        buffer.endIndex - readOffset
    }

    private var unreadData: Data.SubSequence {
        buffer[readOffset..<buffer.endIndex]
    }

    private mutating func consume(through offset: Int) {
        readOffset = offset
        endMarkerSearchOffset = offset
        compact(force: false)
    }

    /// Front-removing every JPEG shifts the remaining Data repeatedly. Retain a read
    /// cursor and compact in larger batches instead.
    private mutating func compact(force: Bool) {
        guard readOffset > 0 else { return }
        guard force || readOffset >= Self.compactionThreshold else { return }
        buffer.removeSubrange(buffer.startIndex..<readOffset)
        endMarkerSearchOffset = max(0, endMarkerSearchOffset - readOffset)
        readOffset = 0
    }

    private static func contentLength(from headerData: Data) -> Int? {
        guard let headers = String(data: headerData, encoding: .isoLatin1) else { return nil }
        for line in headers.split(whereSeparator: \Character.isNewline) {
            let parts = line.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2,
                  parts[0].trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "content-length" else {
                continue
            }
            return Int(parts[1].trimmingCharacters(in: .whitespacesAndNewlines))
        }
        return nil
    }

    private static func validJPEG(from data: Data) -> Data? {
        guard data.starts(with: startMarker), data.count >= 4,
              data.suffix(endMarker.count).elementsEqual(endMarker) else { return nil }
        return data
    }
}

final class MJPEGClient: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    typealias StateHandler = @Sendable (_ connected: Bool, _ reason: String?) -> Void

    private let url: URL
    private let stateHandler: StateHandler
    private let continuation: AsyncStream<JPEGFrame>.Continuation
    let frames: AsyncStream<JPEGFrame>
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var parser = MultipartJPEGParser()
    private let lock = NSLock()
    private var stopped = false
    private var connected = false
    private var reconnectGeneration = 0
    private var droppedFrames = 0
    private var parsedFrames = 0
    private var parsedFrameTimes: [TimeInterval] = []

    init(url: URL, stateHandler: @escaping StateHandler) {
        self.url = url
        self.stateHandler = stateHandler
        var captured: AsyncStream<JPEGFrame>.Continuation!
        // URLSession may deliver several complete JPEGs in one network callback. Keep a
        // short, bounded burst buffer so those frames are processed instead of collapsed.
        self.frames = AsyncStream(bufferingPolicy: .bufferingNewest(16)) { captured = $0 }
        self.continuation = captured
        super.init()
    }

    func start() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 10
        configuration.timeoutIntervalForResource = .infinity
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        let created = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        lock.withLock {
            session = created
            stopped = false
        }
        openStream()
    }

    func stop() {
        let sessionToStop = lock.withLock { () -> URLSession? in
            stopped = true
            reconnectGeneration += 1
            task = nil
            let value = session
            session = nil
            return value
        }
        sessionToStop?.invalidateAndCancel()
        continuation.finish()
    }

    /// Cancel a stream that is still technically open but has stopped producing JPEGs.
    func reconnectStalledStream(reason: String) {
        let taskToCancel = lock.withLock { () -> URLSessionDataTask? in
            guard !stopped else { return nil }
            markDisconnectedLocked()
            let value = task
            task = nil
            return value
        }
        if taskToCancel != nil { stateHandler(false, reason) }
        taskToCancel?.cancel()
        if taskToCancel != nil { scheduleReconnect() }
    }

    func metrics() -> MJPEGMetrics {
        lock.withLock {
            let fps: Double
            if let first = parsedFrameTimes.first, let last = parsedFrameTimes.last, last > first {
                fps = Double(parsedFrameTimes.count - 1) / (last - first)
            } else {
                fps = 0
            }
            return MJPEGMetrics(cameraFPS: fps, parsedFrames: parsedFrames, droppedFrames: droppedFrames)
        }
    }

    private func openStream() {
        let newTask = lock.withLock { () -> URLSessionDataTask? in
            guard !stopped, task == nil, let session else { return nil }
            resetParserLocked()
            var request = URLRequest(url: url)
            request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
            let value = session.dataTask(with: request)
            task = value
            return value
        }
        newTask?.resume()
    }

    private func scheduleReconnect() {
        let generation = lock.withLock { () -> Int? in
            guard !stopped else { return nil }
            reconnectGeneration += 1
            return reconnectGeneration
        }
        guard let generation else { return }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 2) { [weak self] in
            guard let self else { return }
            let shouldOpen = self.lock.withLock {
                !self.stopped && self.reconnectGeneration == generation && self.task == nil
            }
            if shouldOpen { self.openStream() }
        }
    }

    private func resetParserLocked() {
        parser.reset()
    }

    private func markDisconnectedLocked() {
        connected = false
        resetParserLocked()
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            completionHandler(.cancel)
            return
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        var framesToYield: [JPEGFrame] = []
        var becameConnected = false
        lock.withLock {
            guard !stopped, task === dataTask else { return }
            let jpegData = parser.append(data)
            let wallClock = Date()
            var monotonic = ProcessInfo.processInfo.systemUptime
            framesToYield = jpegData.map { jpeg in
                defer { monotonic += 0.000_001 }
                return JPEGFrame(data: jpeg, receivedAt: wallClock, monotonicTime: monotonic)
            }
            for frame in framesToYield {
                parsedFrames += 1
                parsedFrameTimes.append(frame.monotonicTime)
            }
            if let newest = parsedFrameTimes.last {
                parsedFrameTimes.removeAll { newest - $0 > 5 }
                if parsedFrameTimes.count > 120 {
                    parsedFrameTimes.removeFirst(parsedFrameTimes.count - 120)
                }
            }
            if !framesToYield.isEmpty, !connected {
                connected = true
                becameConnected = true
            }
        }
        if becameConnected { stateHandler(true, nil) }
        for frame in framesToYield {
            if case .dropped = continuation.yield(frame) {
                lock.withLock { droppedFrames += 1 }
            }
        }
    }

    func urlSession(_ session: URLSession, task completedTask: URLSessionTask, didCompleteWithError error: (any Error)?) {
        var shouldReconnect = false
        var shouldNotify = false
        lock.withLock {
            guard task === completedTask else { return }
            task = nil
            shouldNotify = connected || error != nil
            markDisconnectedLocked()
            shouldReconnect = !stopped
        }
        if shouldNotify {
            stateHandler(false, error?.localizedDescription ?? "stream closed")
        }
        if shouldReconnect { scheduleReconnect() }
    }
}
