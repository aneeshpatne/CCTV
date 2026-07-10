import Foundation

final class MJPEGClient: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    typealias StateHandler = @Sendable (_ connected: Bool, _ reason: String?) -> Void

    private let url: URL
    private let stateHandler: StateHandler
    private let continuation: AsyncStream<Data>.Continuation
    let frames: AsyncStream<Data>
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var buffer = Data()
    private let lock = NSLock()
    private var stopped = false
    private var connected = false
    private var reconnectGeneration = 0
    private var droppedFrames = 0

    init(url: URL, stateHandler: @escaping StateHandler) {
        self.url = url
        self.stateHandler = stateHandler
        var captured: AsyncStream<Data>.Continuation!
        self.frames = AsyncStream(bufferingPolicy: .bufferingNewest(2)) { captured = $0 }
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

    func droppedFrameCount() -> Int {
        lock.withLock { droppedFrames }
    }

    private func openStream() {
        let newTask = lock.withLock { () -> URLSessionDataTask? in
            guard !stopped, task == nil, let session else { return nil }
            buffer.removeAll(keepingCapacity: true)
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

    private func markDisconnectedLocked() {
        connected = false
        buffer.removeAll(keepingCapacity: true)
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
        var framesToYield: [Data] = []
        var becameConnected = false
        lock.withLock {
            guard !stopped, task === dataTask else { return }
            buffer.append(data)
            let startMarker = Data([0xFF, 0xD8])
            let endMarker = Data([0xFF, 0xD9])

            while let start = buffer.range(of: startMarker) {
                guard let end = buffer.range(of: endMarker, in: start.lowerBound..<buffer.endIndex) else {
                    if start.lowerBound > buffer.startIndex {
                        buffer.removeSubrange(buffer.startIndex..<start.lowerBound)
                    }
                    break
                }
                let frameEnd = end.upperBound
                framesToYield.append(Data(buffer[start.lowerBound..<frameEnd]))
                buffer.removeSubrange(buffer.startIndex..<frameEnd)
            }

            if buffer.count > 8 * 1024 * 1024 {
                buffer.removeFirst(buffer.count - 2 * 1024 * 1024)
            }
            if !framesToYield.isEmpty, !connected {
                connected = true
                becameConnected = true
            }
        }
        if becameConnected { stateHandler(true, nil) }
        for jpeg in framesToYield {
            if case .dropped = continuation.yield(jpeg) {
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
