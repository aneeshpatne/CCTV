import Foundation

final class MJPEGClient: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let url: URL
    private let continuation: AsyncStream<Data>.Continuation
    let frames: AsyncStream<Data>
    private var session: URLSession?
    private var buffer = Data()
    private let lock = NSLock()
    private(set) var droppedFrames = 0

    init(url: URL) {
        self.url = url
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
        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        self.session = session
        var request = URLRequest(url: url)
        request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        session.dataTask(with: request).resume()
    }

    func stop() {
        session?.invalidateAndCancel()
        continuation.finish()
    }

    func droppedFrameCount() -> Int {
        lock.lock()
        defer { lock.unlock() }
        return droppedFrames
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        lock.lock()
        defer { lock.unlock() }
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
            let jpeg = Data(buffer[start.lowerBound..<frameEnd])
            buffer.removeSubrange(buffer.startIndex..<frameEnd)
            if case .dropped = continuation.yield(jpeg) { droppedFrames += 1 }
        }

        if buffer.count > 8 * 1024 * 1024 {
            buffer.removeFirst(buffer.count - 2 * 1024 * 1024)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: (any Error)?) {
        if let error {
            FileHandle.standardError.write(Data("MJPEG stream ended: \(error)\n".utf8))
        }
        continuation.finish()
    }
}
