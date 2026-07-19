import XCTest
@testable import CCTVCapture

final class CCTVCaptureTests: XCTestCase {
    private func pixelBuffer(
        width: Int = 8,
        height: Int = 8,
        blue: UInt8,
        green: UInt8,
        red: UInt8
    ) throws -> CVPixelBuffer {
        var created: CVPixelBuffer?
        XCTAssertEqual(
            CVPixelBufferCreate(
                nil,
                width,
                height,
                kCVPixelFormatType_32BGRA,
                nil,
                &created
            ),
            kCVReturnSuccess
        )
        let buffer = try XCTUnwrap(created)
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        let rowBytes = CVPixelBufferGetBytesPerRow(buffer)
        let bytes = CVPixelBufferGetBaseAddress(buffer)!.assumingMemoryBound(to: UInt8.self)
        for y in 0..<height {
            for x in 0..<width {
                let offset = y * rowBytes + x * 4
                bytes[offset] = blue
                bytes[offset + 1] = green
                bytes[offset + 2] = red
                bytes[offset + 3] = 255
            }
        }
        return buffer
    }

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

    func testCameraSettingsSummarizeAuthoritativeManualProfile() {
        let settings = CameraSettings(
            framesize: 12,
            xclk: 20,
            autoExposure: false,
            shutterLines: 300,
            autoGain: false,
            gainX16: 24,
            gainRegister: 8,
            autoWhiteBalance: false,
            red: 94,
            green: 65,
            blue: 84,
            saturationU: 72,
            saturationV: 72,
            cachedForRecovery: true
        )

        XCTAssertEqual(
            settings.imageSummary,
            "XCLK 20 · AE MANUAL · 300L · AGC MANUAL · GAIN 24/16 · REG 8 · AWB MANUAL · WB 94/65/84 · SAT 72/72"
        )
    }

    func testImageMetricsEventCarriesBrightnessWithoutChangingProtocolVersion() throws {
        let event = WorkerEvent(
            type: "image.metrics",
            payload: .imageMetrics(
                sceneBrightness: 0.21,
                redOverGreen: 0.94,
                blueOverGreen: 1.08
            )
        )
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(event)) as? [String: Any]
        )
        XCTAssertEqual(object["version"] as? Int, 1)
        XCTAssertEqual(object["type"] as? String, "image.metrics")
        let payload = try XCTUnwrap(object["payload"] as? [String: Any])
        XCTAssertEqual(payload["scene_brightness"] as? Double, 0.21)
        XCTAssertEqual(payload["red_over_green"] as? Double, 0.94)
        XCTAssertEqual(payload["blue_over_green"] as? Double, 1.08)
    }

    func testImageMetricsUseNeutralPixelRatios() throws {
        let buffer = try pixelBuffer(blue: 90, green: 100, red: 110)
        let metrics = try XCTUnwrap(MotionDetector.imageMetrics(buffer))
        XCTAssertEqual(try XCTUnwrap(metrics.redOverGreen), 1.1, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(metrics.blueOverGreen), 0.9, accuracy: 0.0001)
    }

    func testImageMetricsReferenceReportsStrongColorCast() throws {
        let buffer = try pixelBuffer(blue: 20, green: 40, red: 180)
        let metrics = try XCTUnwrap(MotionDetector.imageMetrics(buffer))
        XCTAssertEqual(try XCTUnwrap(metrics.redOverGreen), 4.5, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(metrics.blueOverGreen), 0.5, accuracy: 0.0001)
    }

    func testImageMetricsFilterDoesNotSmoothFreshChroma() throws {
        var filter = ImageMetricsFilter()
        _ = filter.update(brightness: 0.2, redRatio: 0.8, blueRatio: 1.2)
        let current = filter.update(brightness: 0.4, redRatio: 1.1, blueRatio: 0.9)
        XCTAssertEqual(try XCTUnwrap(current.1), 1.1, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(current.2), 0.9, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(current.0), 0.22, accuracy: 0.0001)
    }

    func testImageMetricsFilterClearsStaleChromaWhenNeutralPixelsDisappear() throws {
        var filter = ImageMetricsFilter()
        _ = filter.update(brightness: 0.3, redRatio: 1.08, blueRatio: 0.93)
        let missing = filter.update(brightness: 0.3, redRatio: nil, blueRatio: nil)
        XCTAssertNil(missing.1)
        XCTAssertNil(missing.2)
        XCTAssertNotNil(missing.0)
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

    func testMotionActivityStaysActiveUntilTenQuietSeconds() {
        var guardState = MotionActivityGuard(holdDuration: 10)

        let started = guardState.update(candidate: true, at: 100)
        XCTAssertTrue(started.active)
        XCTAssertTrue(started.started)

        let quiet = guardState.update(candidate: false, at: 109.9)
        XCTAssertTrue(quiet.active)
        XCTAssertFalse(quiet.started)

        let expired = guardState.update(candidate: false, at: 110)
        XCTAssertFalse(expired.active)
        XCTAssertFalse(expired.started)
    }

    func testMotionActivityExtendsWithoutRestartingBlink() {
        var guardState = MotionActivityGuard(holdDuration: 10)

        XCTAssertTrue(guardState.update(candidate: true, at: 100).started)
        let renewed = guardState.update(candidate: true, at: 109)
        XCTAssertTrue(renewed.active)
        XCTAssertFalse(renewed.started)
        XCTAssertTrue(guardState.update(candidate: false, at: 118.9).active)
        XCTAssertFalse(guardState.update(candidate: false, at: 119).active)
        XCTAssertTrue(guardState.update(candidate: true, at: 119.1).started)
    }

    func testCameraLEDBlinkPatternIsGuardedQuickDoubleFlash() {
        XCTAssertEqual(CameraLEDBlinker.pattern, [
            LEDBlinkStep(brightness: 10, duration: 0.2),
            LEDBlinkStep(brightness: 0, duration: 0.2),
            LEDBlinkStep(brightness: 10, duration: 0.2),
            LEDBlinkStep(brightness: 0, duration: 1.0),
        ])
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

    func testMultipartParserHandlesLargeJPEGOneByteAtATime() {
        var jpeg = Data([0xFF, 0xD8])
        jpeg.append(Data(repeating: 0x55, count: 512 * 1024))
        jpeg.append(contentsOf: [0xFF, 0xD9])

        var parser = MultipartJPEGParser()
        var parsed: [Data] = []
        for byte in jpeg {
            parsed.append(contentsOf: parser.append(Data([byte])))
        }

        XCTAssertEqual(parsed, [jpeg])
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
                redOverGreen: 0.95,
                blueOverGreen: 1.05,
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
