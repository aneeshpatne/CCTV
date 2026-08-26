import Foundation

struct PipelineConfiguration: Sendable {
    let cameraBaseURL: URL
    let streamURL: URL
    let recordingsDirectory: URL
    let rtspURL: String
    let targetFPS: Double
    let segmentSeconds: Double
    let localBitrate: Int
    let rtspBitrate: Int
    let eventFileDescriptor: Int32?
    let faces: FaceConfiguration

    static func load(environment: [String: String] = ProcessInfo.processInfo.environment) throws -> Self {
        let baseString = environment["ESP32CAM_BASE_URL"] ?? "http://192.168.0.13"
        guard let base = URL(string: baseString) else {
            throw ConfigurationError.invalidURL("ESP32CAM_BASE_URL", baseString)
        }
        let streamString = environment["ESP32CAM_STREAM_URL"] ?? "\(baseString):81/stream"
        guard let stream = URL(string: streamString) else {
            throw ConfigurationError.invalidURL("ESP32CAM_STREAM_URL", streamString)
        }

        let recordings = environment["CCTV_RECORDINGS_DIR"]
            ?? "/Volumes/HP USB20FD/CCTV/recordings/esp_cam1"
        let fps = Double(environment["CCTV_TARGET_FPS"] ?? "9") ?? 9
        let segment = Double(environment["CCTV_SEGMENT_SECONDS"] ?? "60") ?? 60
        let localBitrate = Int(environment["CCTV_HEVC_BITRATE"] ?? "500000") ?? 500_000
        let rtspBitrate = Int(environment["CCTV_RTSP_BITRATE"] ?? "1500000") ?? 1_500_000
        let fd = environment["CCTV_EVENT_FD"].flatMap(Int32.init)

        guard fps > 0, segment >= 10, localBitrate > 0, rtspBitrate > 0 else {
            throw ConfigurationError.invalidNumericValue
        }

        return Self(
            cameraBaseURL: base,
            streamURL: stream,
            recordingsDirectory: URL(fileURLWithPath: recordings, isDirectory: true),
            rtspURL: environment["CCTV_RTSP_URL"] ?? "rtsp://127.0.0.1:8554/esp_cam1_overlay",
            targetFPS: fps,
            segmentSeconds: segment,
            localBitrate: localBitrate,
            rtspBitrate: rtspBitrate,
            eventFileDescriptor: fd,
            faces: FaceConfiguration.load(environment: environment)
        )
    }
}

enum ConfigurationError: Error, CustomStringConvertible {
    case invalidURL(String, String)
    case invalidNumericValue

    var description: String {
        switch self {
        case let .invalidURL(key, value): return "invalid \(key): \(value)"
        case .invalidNumericValue: return "FPS, segment duration, and bitrates must be positive"
        }
    }
}
