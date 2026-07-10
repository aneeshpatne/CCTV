import XCTest
@testable import CCTVCapture

final class CCTVCaptureTests: XCTestCase {
    func testConfigurationDefaults() throws {
        let configuration = try PipelineConfiguration.load(environment: [:])
        XCTAssertEqual(configuration.targetFPS, 9)
        XCTAssertEqual(configuration.segmentSeconds, 60)
        XCTAssertEqual(configuration.localBitrate, 500_000)
        XCTAssertEqual(configuration.rtspURL, "rtsp://127.0.0.1:8554/esp_cam1_overlay")
    }

    func testConfigurationOverrides() throws {
        let configuration = try PipelineConfiguration.load(environment: [
            "ESP32CAM_BASE_URL": "http://127.0.0.1",
            "ESP32CAM_STREAM_URL": "http://127.0.0.1:8080/stream",
            "CCTV_RECORDINGS_DIR": "/tmp/cctv-native-tests",
            "CCTV_TARGET_FPS": "12",
            "CCTV_SEGMENT_SECONDS": "120",
        ])
        XCTAssertEqual(configuration.targetFPS, 12)
        XCTAssertEqual(configuration.segmentSeconds, 120)
        XCTAssertEqual(configuration.streamURL.port, 8080)
    }

    func testMotionAccumulatorRequiresPersistenceAndKeepsPadding() async {
        let accumulator = MotionEventAccumulator(cooldown: 1)
        let start = Date(timeIntervalSince1970: 1_000)
        let first = await accumulator.update(candidate: true, confidence: 0.7, semanticLabels: [], at: start)
        let second = await accumulator.update(candidate: true, confidence: 0.8, semanticLabels: [SemanticLabel(name: "person", confidence: 0.9)], at: start.addingTimeInterval(0.1))
        let third = await accumulator.update(candidate: false, confidence: 0, semanticLabels: [], at: start.addingTimeInterval(0.5))
        XCTAssertNil(first)
        XCTAssertNil(second)
        XCTAssertNil(third)
        let event = await accumulator.update(candidate: false, confidence: 0, semanticLabels: [], at: start.addingTimeInterval(1.6))
        XCTAssertNotNil(event)
    }
}
