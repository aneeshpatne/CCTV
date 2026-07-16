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

    func testStreamStateEventProtocol() throws {
        let event = WorkerEvent(
            type: "stream.disconnected",
            payload: .stream(connected: false, reason: "stalled")
        )
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(event)) as? [String: Any]
        )
        XCTAssertEqual(object["version"] as? Int, 1)
        XCTAssertEqual(object["type"] as? String, "stream.disconnected")
        let payload = try XCTUnwrap(object["payload"] as? [String: Any])
        XCTAssertEqual(payload["connected"] as? Bool, false)
        XCTAssertEqual(payload["reason"] as? String, "stalled")
    }

    func testMultipartParserHandlesFragmentedHeadersAndMultipleFrames() {
        let first = Data([0xFF, 0xD8, 1, 2, 3, 0xFF, 0xD9])
        let second = Data([0xFF, 0xD8, 4, 5, 0xFF, 0xD9])
        func part(_ jpeg: Data) -> Data {
            var data = Data("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: \(jpeg.count)\r\n\r\n".utf8)
            data.append(jpeg)
            data.append(Data("\r\n".utf8))
            return data
        }
        let stream = part(first) + part(second)
        let chunkSizes = [1, 2, 5, 3, 11, 7, 19, 4, 64]
        var parser = MultipartJPEGParser()
        var parsed: [Data] = []
        var offset = 0
        for size in chunkSizes where offset < stream.count {
            let end = min(stream.count, offset + size)
            parsed.append(contentsOf: parser.append(Data(stream[offset..<end])))
            offset = end
        }
        if offset < stream.count {
            parsed.append(contentsOf: parser.append(Data(stream[offset...])))
        }
        XCTAssertEqual(parsed, [first, second])
    }

    func testMultipartParserRetainsMarkerFallback() {
        let jpeg = Data([0xFF, 0xD8, 9, 8, 7, 0xFF, 0xD9])
        var stream = Data("--frame\r\nContent-Type: image/jpeg\r\n\r\n".utf8)
        stream.append(jpeg)
        var parser = MultipartJPEGParser()
        XCTAssertTrue(parser.append(Data(stream.prefix(13))).isEmpty)
        XCTAssertEqual(parser.append(Data(stream.dropFirst(13))), [jpeg])
        parser.reset()
        XCTAssertEqual(parser.append(Data("noise".utf8) + jpeg), [jpeg])
    }

    func testCameraTimelinePreservesVariableArrivalTimes() {
        var timeline = CameraPresentationTimeline(originUptime: 100)
        let times = [100.0, 100.11, 100.43, 100.52].map {
            timeline.presentationTime(for: $0).seconds
        }
        XCTAssertEqual(times[0], 0, accuracy: 0.000_01)
        XCTAssertEqual(times[1], 0.11, accuracy: 0.000_02)
        XCTAssertEqual(times[2], 0.43, accuracy: 0.000_02)
        XCTAssertEqual(times[3], 0.52, accuracy: 0.000_02)
    }

    func testRTSPPublisherUsesWallClockVFRInput() {
        let arguments = RTSPPublisher.ffmpegArguments(rtspURL: "rtsp://127.0.0.1/test")
        XCTAssertTrue(arguments.contains("-use_wallclock_as_timestamps"))
        XCTAssertTrue(arguments.contains("passthrough"))
        XCTAssertFalse(arguments.contains("-r"))
    }

    func testLateSemanticLabelsMergeIntoActiveMotionEvent() async throws {
        let accumulator = MotionEventAccumulator(cooldown: 1)
        let start = Date(timeIntervalSince1970: 2_000)
        _ = await accumulator.update(candidate: true, confidence: 0.5, semanticLabels: [], at: start)
        _ = await accumulator.update(candidate: true, confidence: 0.6, semanticLabels: [], at: start.addingTimeInterval(0.1))
        await accumulator.merge(semanticLabels: [SemanticLabel(name: "animal", confidence: 0.8)])
        _ = await accumulator.update(candidate: false, confidence: 0, semanticLabels: [], at: start.addingTimeInterval(0.2))
        let finalized = await accumulator.update(
            candidate: false,
            confidence: 0,
            semanticLabels: [],
            at: start.addingTimeInterval(1.2)
        )
        let event = try XCTUnwrap(finalized)
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(event)) as? [String: Any]
        )
        let payload = try XCTUnwrap(object["payload"] as? [String: Any])
        let labels = try XCTUnwrap(payload["labels"] as? [[String: Any]])
        XCTAssertEqual(labels.first?["name"] as? String, "animal")
    }

    func testHealthEventAddsVFRMetricsWithoutChangingVersion() throws {
        let event = WorkerEvent(
            type: "health",
            payload: .health(
                fps: 10,
                cameraFPS: 11,
                outputFPS: 10,
                droppedFrames: 1,
                encoderDroppedFrames: 2,
                processingLatencyMS: 12.5,
                motionScore: 0.1,
                sceneBrightness: 0.42,
                recording: true,
                rtsp: true
            )
        )
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(event)) as? [String: Any]
        )
        XCTAssertEqual(object["version"] as? Int, 1)
        let payload = try XCTUnwrap(object["payload"] as? [String: Any])
        XCTAssertEqual(payload["camera_fps"] as? Double, 11)
        XCTAssertEqual(payload["output_fps"] as? Double, 10)
        XCTAssertEqual(payload["encoder_dropped_frames"] as? Int, 2)
        XCTAssertEqual(payload["scene_brightness"] as? Double, 0.42)
    }
}
