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
        let third = await accumulator.update(candidate: true, confidence: 0.85, semanticLabels: [], at: start.addingTimeInterval(0.2))
        let quiet = await accumulator.update(candidate: false, confidence: 0, semanticLabels: [], at: start.addingTimeInterval(0.5))
        XCTAssertNil(first)
        XCTAssertNil(second)
        XCTAssertNil(third)
        XCTAssertNil(quiet)
        let event = await accumulator.update(candidate: false, confidence: 0, semanticLabels: [], at: start.addingTimeInterval(1.6))
        XCTAssertNotNil(event)
    }

    func testMotionActivityIgnoresSingleFrameNoise() {
        var guardState = MotionActivityGuard(holdDuration: 10)

        let first = guardState.update(candidate: true, at: 100)
        XCTAssertFalse(first.active)
        XCTAssertFalse(first.started)

        let second = guardState.update(candidate: true, at: 100.1)
        XCTAssertFalse(second.active)
        XCTAssertFalse(second.started)

        let third = guardState.update(candidate: true, at: 100.2)
        XCTAssertTrue(third.active)
        XCTAssertTrue(third.started)
    }

    func testMotionActivityStaysActiveUntilTenQuietSeconds() {
        var guardState = MotionActivityGuard(holdDuration: 10)

        _ = guardState.update(candidate: true, at: 99.8)
        _ = guardState.update(candidate: true, at: 99.9)
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

        _ = guardState.update(candidate: true, at: 99.8)
        _ = guardState.update(candidate: true, at: 99.9)
        XCTAssertTrue(guardState.update(candidate: true, at: 100).started)
        let renewed = guardState.update(candidate: true, at: 109)
        XCTAssertTrue(renewed.active)
        XCTAssertFalse(renewed.started)
        XCTAssertTrue(guardState.update(candidate: false, at: 118.9).active)
        XCTAssertFalse(guardState.update(candidate: false, at: 119).active)
        _ = guardState.update(candidate: true, at: 119.0)
        _ = guardState.update(candidate: true, at: 119.05)
        XCTAssertTrue(guardState.update(candidate: true, at: 119.1).started)
    }

    func testMotionActivityIgnoresIntermittentFlickerPattern() {
        var guardState = MotionActivityGuard(holdDuration: 10)
        for index in 0..<10 {
            let state = guardState.update(candidate: index % 2 == 0, at: 100 + Double(index) * 0.1)
            XCTAssertFalse(state.active)
            XCTAssertFalse(state.started)
        }
    }

    func testMotionScoringRejectsScatteredNoise() {
        // Sparse single-block hits with barely-threshold magnitudes look like
        // sensor/JPEG noise rather than an object.
        let cells = (0..<12).map { index in
            (x: (index * 3) % 24, y: (index * 5) % 18, magnitude: 14)
        }
        let result = MotionDetector.evaluate(
            activeCells: cells,
            gridWidth: 32,
            gridHeight: 24,
            eligible: 700
        )
        XCTAssertFalse(result.candidate)
    }

    func testMotionScoringRejectsRibbonFlickerBand() {
        // Full-width one-row band is typical of AC light frequency / rolling flicker.
        let cells = (0..<24).map { x in (x: x, y: 10, magnitude: 22) }
        let result = MotionDetector.evaluate(
            activeCells: cells,
            gridWidth: 32,
            gridHeight: 24,
            eligible: 700
        )
        XCTAssertFalse(result.candidate)
    }

    func testMotionScoringRejectsMultiRowHorizontalBand() {
        // Rolling-shutter AC banding is often 2–4 block-rows tall and nearly full width.
        var cells: [(x: Int, y: Int, magnitude: Int)] = []
        for y in 10..<14 {
            for x in 2..<30 {
                cells.append((x: x, y: y, magnitude: 24))
            }
        }
        let result = MotionDetector.evaluate(
            activeCells: cells,
            gridWidth: 32,
            gridHeight: 24,
            eligible: 700
        )
        XCTAssertFalse(result.candidate)
    }

    func testMotionScoringRejectsIncoherentVectorDirections() {
        // Dense cluster with random directions is light-frequency / noise, not an object.
        var cells: [(x: Int, y: Int, dx: Int, dy: Int)] = []
        let directions = [(20, 0), (-20, 0), (0, 20), (0, -20), (14, 14), (-14, 14), (14, -14), (-14, -14)]
        var index = 0
        for y in 8..<12 {
            for x in 10..<15 {
                let direction = directions[index % directions.count]
                cells.append((x: x, y: y, dx: direction.0, dy: direction.1))
                index += 1
            }
        }
        let result = MotionDetector.evaluate(
            activeCells: cells,
            gridWidth: 32,
            gridHeight: 24,
            eligible: 700
        )
        XCTAssertFalse(result.candidate)
        XCTAssertLessThan(
            MotionDetector.directionCoherence(cells.map { ($0.dx, $0.dy) }),
            MotionScoring.minDirectionCoherence
        )
    }

    func testMotionScoringAcceptsCompactObjectMotion() {
        var cells: [(x: Int, y: Int, magnitude: Int)] = []
        for y in 8..<12 {
            for x in 10..<15 {
                cells.append((x: x, y: y, magnitude: 24))
            }
        }
        let result = MotionDetector.evaluate(
            activeCells: cells,
            gridWidth: 32,
            gridHeight: 24,
            eligible: 700
        )
        XCTAssertTrue(result.candidate)
        XCTAssertNotNil(result.boundingBox)
        XCTAssertGreaterThan(result.score, 0.02)
    }

    func testMotionScoringRejectsNearGlobalLightingChange() {
        var cells: [(x: Int, y: Int, magnitude: Int)] = []
        for y in 0..<24 {
            for x in 0..<20 {
                cells.append((x: x, y: y, magnitude: 30))
            }
        }
        let result = MotionDetector.evaluate(
            activeCells: cells,
            gridWidth: 32,
            gridHeight: 24,
            eligible: 700
        )
        XCTAssertFalse(result.candidate)
        XCTAssertGreaterThanOrEqual(result.score, MotionScoring.maxActiveFraction)
    }

    func testCameraLEDBlinkPatternIsGuardedQuickDoubleFlash() {
        XCTAssertEqual(CameraLEDBlinker.pattern, [
            LEDBlinkStep(brightness: 10, duration: 0.2),
            LEDBlinkStep(brightness: 0, duration: 0.2),
            LEDBlinkStep(brightness: 10, duration: 0.2),
            LEDBlinkStep(brightness: 0, duration: 1.0),
        ])
    }

    func testMotionStartedEventProtocol() throws {
        let event = WorkerEvent(type: "motion.started", payload: .motionStarted)
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(event)) as? [String: Any]
        )
        XCTAssertEqual(object["version"] as? Int, 1)
        XCTAssertEqual(object["type"] as? String, "motion.started")
        XCTAssertEqual((object["payload"] as? [String: Any])?.count, 0)
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
        _ = await accumulator.update(candidate: true, confidence: 0.65, semanticLabels: [], at: start.addingTimeInterval(0.15))
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

    func testFaceConfigurationDefaultsEnableRecognition() throws {
        let configuration = try PipelineConfiguration.load(environment: [:])
        XCTAssertTrue(configuration.faces.enabled)
        XCTAssertEqual(configuration.faces.minHits, 2)
        XCTAssertEqual(configuration.faces.maxIdentities, 32)
        XCTAssertEqual(configuration.faces.minSize, 24, accuracy: 0.0001)
        XCTAssertEqual(configuration.faces.minQuality, 0.05, accuracy: 0.0001)
        XCTAssertEqual(configuration.faces.matchThreshold, 0.52, accuracy: 0.0001)
    }

    func testFaceConfigurationCanBeDisabled() throws {
        let configuration = try PipelineConfiguration.load(environment: [
            "CCTV_FACE_RECOGNITION": "0",
            "CCTV_FACE_MIN_HITS": "4",
            "CCTV_FACE_MAX_IDENTITIES": "8",
        ])
        XCTAssertFalse(configuration.faces.enabled)
        XCTAssertEqual(configuration.faces.minHits, 4)
        XCTAssertEqual(configuration.faces.maxIdentities, 8)
    }

    func testIdentityLabelDetectionIgnoresPerson() {
        XCTAssertTrue(SemanticLabel(name: "p3", confidence: 1).isAutoIdentity)
        XCTAssertTrue(SemanticLabel(name: "p12", confidence: 1).isAutoIdentity)
        XCTAssertFalse(SemanticLabel(name: "person", confidence: 1).isAutoIdentity)
        XCTAssertFalse(SemanticLabel(name: "animal", confidence: 1).isAutoIdentity)
    }

    func testFaceEngineRequiresThreeAgreeingUnknownsToEnroll() {
        var engine = FaceDecisionEngine(configuration: testFaceConfiguration(maxIdentities: 8))
        let start = Date(timeIntervalSince1970: 4_000)
        let vector = [1.0, 0.0, 0.0, 0.0]
        XCTAssertEqual(engine.observe(embedding: vector, quality: 0.8, at: start), .pending)
        XCTAssertEqual(
            engine.observe(embedding: vector, quality: 0.8, at: start.addingTimeInterval(0.2)),
            .pending
        )
        guard case let .enrolled(id, _, embedding) = engine.observe(
            embedding: vector,
            quality: 0.9,
            at: start.addingTimeInterval(0.4)
        ) else {
            return XCTFail("expected enrollment on the third agreeing hit")
        }
        XCTAssertEqual(id, 1)
        XCTAssertEqual(embedding.count, 4)
        XCTAssertEqual(engine.identities.count, 1)
    }

    func testFaceEngineDoesNotEnrollDisagreeingUnknowns() {
        var engine = FaceDecisionEngine(configuration: testFaceConfiguration())
        let start = Date(timeIntervalSince1970: 5_000)
        XCTAssertEqual(engine.observe(embedding: [1, 0, 0, 0], quality: 0.8, at: start), .pending)
        XCTAssertEqual(
            engine.observe(embedding: [0, 1, 0, 0], quality: 0.8, at: start.addingTimeInterval(0.2)),
            .pending
        )
        XCTAssertEqual(
            engine.observe(embedding: [0, 0, 1, 0], quality: 0.8, at: start.addingTimeInterval(0.4)),
            .pending
        )
        XCTAssertTrue(engine.identities.isEmpty)
    }

    func testFaceEngineMatchesAfterThreeHitsAndEmitsOnce() {
        var engine = FaceDecisionEngine(
            configuration: testFaceConfiguration(),
            identities: [
                FaceIdentityRecord(id: 3, embeddings: [[1, 0, 0, 0]], qualities: [0.9]),
            ],
            nextID: 4
        )
        let start = Date(timeIntervalSince1970: 6_000)
        let vector = [1.0, 0.02, 0.0, 0.0]
        XCTAssertEqual(engine.observe(embedding: vector, quality: 0.8, at: start), .pending)
        XCTAssertEqual(
            engine.observe(embedding: vector, quality: 0.8, at: start.addingTimeInterval(0.2)),
            .pending
        )
        guard case let .matched(id, confidence, emit, record) = engine.observe(
            embedding: vector,
            quality: 0.85,
            at: start.addingTimeInterval(0.4)
        ) else {
            return XCTFail("expected a match after three hits")
        }
        XCTAssertEqual(id, 3)
        XCTAssertGreaterThan(confidence, 0.9)
        XCTAssertTrue(emit)
        XCTAssertTrue(record)
        guard case let .matched(_, _, emitAgain, _) = engine.observe(
            embedding: vector,
            quality: 0.8,
            at: start.addingTimeInterval(0.6)
        ) else {
            return XCTFail("expected continued match")
        }
        XCTAssertFalse(emitAgain)
    }

    func testFaceEngineRejectsCloseSecondBestMatch() {
        var engine = FaceDecisionEngine(
            configuration: testFaceConfiguration(margin: 0.08),
            identities: [
                FaceIdentityRecord(id: 1, embeddings: [[1, 0.05, 0, 0]], qualities: [0.8]),
                FaceIdentityRecord(id: 2, embeddings: [[1, 0.0, 0, 0]], qualities: [0.8]),
            ],
            nextID: 3
        )
        let outcome = engine.observe(
            embedding: [1, 0.02, 0, 0],
            quality: 0.9,
            at: Date(timeIntervalSince1970: 7_000)
        )
        XCTAssertEqual(outcome, .pending)
        XCTAssertEqual(engine.identities.count, 2)
    }

    func testFaceEngineHonorsIdentityCap() {
        var engine = FaceDecisionEngine(
            configuration: testFaceConfiguration(maxIdentities: 1),
            identities: [
                FaceIdentityRecord(id: 1, embeddings: [[1, 0, 0, 0]], qualities: [0.9]),
            ],
            nextID: 2
        )
        let start = Date(timeIntervalSince1970: 8_000)
        let vector = [0.0, 1.0, 0.0, 0.0]
        XCTAssertEqual(engine.observe(embedding: vector, quality: 0.8, at: start), .pending)
        XCTAssertEqual(engine.observe(embedding: vector, quality: 0.8, at: start.addingTimeInterval(0.2)), .pending)
        XCTAssertEqual(
            engine.observe(embedding: vector, quality: 0.8, at: start.addingTimeInterval(0.4)),
            .rejectedAtCapacity
        )
        XCTAssertEqual(engine.identities.map(\.id), [1])
    }

    func testFaceGalleryRejectsMismatchedEmbedder() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("cctv-face-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        FaceGalleryStore.save(
            FaceGalleryFile(
                version: 1,
                embedder: "other-embedder",
                nextID: 4,
                identities: [FaceIdentityRecord(id: 3, embeddings: [[1, 0]], qualities: [1])]
            ),
            to: directory
        )
        XCTAssertNil(
            FaceGalleryStore.load(directory: directory, expectedEmbedder: FaceConfiguration.featurePrintEmbedder)
        )
    }

    func testFaceEnrolledEventKeepsProtocolVersion1() throws {
        let event = WorkerEvent(
            type: "face.enrolled",
            payload: .faceEnrolled(
                id: 3,
                confidence: 1,
                quality: 0.8,
                cropPath: "/tmp/p3.jpg",
                embedding: [1, 0, 0],
                embedder: FaceConfiguration.featurePrintEmbedder
            )
        )
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(event)) as? [String: Any]
        )
        XCTAssertEqual(object["version"] as? Int, 1)
        XCTAssertEqual(object["type"] as? String, "face.enrolled")
        let payload = try XCTUnwrap(object["payload"] as? [String: Any])
        XCTAssertEqual(payload["id"] as? Int, 3)
        XCTAssertEqual(payload["crop_path"] as? String, "/tmp/p3.jpg")
        XCTAssertEqual(payload["embedder"] as? String, FaceConfiguration.featurePrintEmbedder)
    }

    func testFaceMatchedEventKeepsProtocolVersion1() throws {
        let event = WorkerEvent(
            type: "face.matched",
            payload: .faceMatched(id: 2, confidence: 0.88)
        )
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(event)) as? [String: Any]
        )
        XCTAssertEqual(object["version"] as? Int, 1)
        let payload = try XCTUnwrap(object["payload"] as? [String: Any])
        XCTAssertEqual(payload["id"] as? Int, 2)
        XCTAssertEqual(payload["confidence"] as? Double, 0.88)
    }

    private func testFaceConfiguration(
        maxIdentities: Int = 32,
        margin: Double = 0.08
    ) -> FaceConfiguration {
        FaceConfiguration(
            enabled: true,
            galleryDirectory: URL(fileURLWithPath: "/tmp/cctv-faces", isDirectory: true),
            matchThreshold: 0.75,
            matchMargin: margin,
            minHits: 3,
            minSize: 48,
            minQuality: 0.3,
            maxIdentities: maxIdentities,
            maxExemplars: 8
        )
    }
}
