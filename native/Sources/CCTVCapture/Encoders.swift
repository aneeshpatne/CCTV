@preconcurrency import AVFoundation
import CoreMedia
import CoreVideo
import Foundation
import VideoToolbox

private final class AssetWriterBox: @unchecked Sendable {
    let writer: AVAssetWriter
    init(_ writer: AVAssetWriter) { self.writer = writer }
}

final class SegmentRecorder: @unchecked Sendable {
    private let directory: URL
    private let targetFPS: Double
    private let segmentSeconds: Double
    private let bitrate: Int
    private let emitter: EventEmitter
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var adaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var segmentStartPTS: CMTime?
    private var segmentStartDate: Date?
    private var partialURL: URL?

    init(configuration: PipelineConfiguration, emitter: EventEmitter) throws {
        self.directory = configuration.recordingsDirectory
        self.targetFPS = configuration.targetFPS
        self.segmentSeconds = configuration.segmentSeconds
        self.bitrate = configuration.localBitrate
        self.emitter = emitter
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    var isRecording: Bool { writer?.status == .writing }

    @discardableResult
    func append(_ pixelBuffer: CVPixelBuffer, presentationTime: CMTime, wallClock: Date) -> Bool {
        if let start = segmentStartPTS,
           presentationTime.seconds - start.seconds >= segmentSeconds {
            finishCurrent(at: wallClock)
        }
        if writer == nil {
            do {
                try startSegment(pixelBuffer: pixelBuffer, presentationTime: presentationTime, wallClock: wallClock)
            } catch {
                FileHandle.standardError.write(Data("segment start failed: \(error)\n".utf8))
                return false
            }
        }
        guard let writer, writer.status == .writing, let input else { return false }
        if !input.isReadyForMoreMediaData {
            // AVAssetWriter can briefly report backpressure while flushing a fragment.
            // The camera interval is much larger than this bounded wait, so preserving
            // the fresh frame here does not create an unbounded queue or visible latency.
            let deadline = ProcessInfo.processInfo.systemUptime + 0.025
            repeat {
                Thread.sleep(forTimeInterval: 0.001)
            } while !input.isReadyForMoreMediaData
                && writer.status == .writing
                && ProcessInfo.processInfo.systemUptime < deadline
        }
        guard writer.status == .writing, input.isReadyForMoreMediaData else {
            FileHandle.standardError.write(Data("segment append skipped after writer backpressure timeout\n".utf8))
            return false
        }
        if adaptor?.append(pixelBuffer, withPresentationTime: presentationTime) != true {
            FileHandle.standardError.write(Data("segment append failed: \(writer.error?.localizedDescription ?? "unknown")\n".utf8))
            return false
        }
        return true
    }

    func finish(at wallClock: Date = Date()) {
        finishCurrent(at: wallClock)
    }

    private func startSegment(pixelBuffer: CVPixelBuffer, presentationTime: CMTime, wallClock: Date) throws {
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let filename = "recording_\(Self.filenameFormatter.string(from: wallClock)).mp4"
        let partial = directory.appendingPathComponent(filename + ".partial")
        try? FileManager.default.removeItem(at: partial)
        let writer = try AVAssetWriter(outputURL: partial, fileType: .mp4)
        writer.shouldOptimizeForNetworkUse = true
        writer.movieFragmentInterval = CMTime(seconds: 2, preferredTimescale: 600)

        let hardwareSpecification = [
            kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder as String: true,
        ]
        let compression: [String: Any] = [
            AVVideoAverageBitRateKey: bitrate,
            AVVideoExpectedSourceFrameRateKey: Int(targetFPS.rounded()),
            AVVideoMaxKeyFrameIntervalKey: Int(targetFPS.rounded()),
            AVVideoAllowFrameReorderingKey: true,
            AVVideoQualityKey: 0.60,
        ]
        let settings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.hevc,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: compression,
            AVVideoEncoderSpecificationKey: hardwareSpecification,
            AVVideoColorPropertiesKey: [
                AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
                AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2,
                AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2,
            ],
        ]
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        input.expectsMediaDataInRealTime = true
        guard writer.canAdd(input) else { throw EncoderError.cannotAddWriterInput }
        writer.add(input)
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: input,
            sourcePixelBufferAttributes: [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                kCVPixelBufferWidthKey as String: width,
                kCVPixelBufferHeightKey as String: height,
                kCVPixelBufferMetalCompatibilityKey as String: true,
            ]
        )
        guard writer.startWriting() else { throw writer.error ?? EncoderError.writerStart }
        writer.startSession(atSourceTime: presentationTime)
        self.writer = writer
        self.input = input
        self.adaptor = adaptor
        self.segmentStartPTS = presentationTime
        self.segmentStartDate = wallClock
        self.partialURL = partial
    }

    private func finishCurrent(at endDate: Date) {
        guard let writer, let input, let partialURL, let startDate = segmentStartDate else { return }
        input.markAsFinished()
        self.writer = nil
        self.input = nil
        self.adaptor = nil
        self.segmentStartPTS = nil
        self.segmentStartDate = nil
        self.partialURL = nil

        let writerBox = AssetWriterBox(writer)
        writer.finishWriting { [emitter, writerBox] in
            guard writerBox.writer.status == .completed else {
                FileHandle.standardError.write(Data("segment finalize failed: \(writerBox.writer.error?.localizedDescription ?? "unknown")\n".utf8))
                return
            }
            let finalName = partialURL.lastPathComponent.replacingOccurrences(of: ".partial", with: "")
            let finalURL = partialURL.deletingLastPathComponent().appendingPathComponent(finalName)
            do {
                try? FileManager.default.removeItem(at: finalURL)
                try FileManager.default.moveItem(at: partialURL, to: finalURL)
                let attributes = try FileManager.default.attributesOfItem(atPath: finalURL.path)
                let size = (attributes[.size] as? NSNumber)?.int64Value ?? 0
                emitter.emit(WorkerEvent(
                    type: "segment.finalized",
                    payload: .segment(
                        path: finalURL.path,
                        start: startDate.timeIntervalSince1970,
                        end: endDate.timeIntervalSince1970,
                        duration: endDate.timeIntervalSince(startDate),
                        codec: "hevc",
                        size: size
                    )
                ))
            } catch {
                FileHandle.standardError.write(Data("segment rename failed: \(error)\n".utf8))
            }
        }
    }

    private static let filenameFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "Asia/Kolkata")
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        return formatter
    }()
}

final class RTSPPublisher: @unchecked Sendable {
    private let rtspURL: String
    private let lock = NSLock()
    private let writerQueue = DispatchQueue(label: "cctv.rtsp-writer", qos: .utility)
    private var process: Process?
    private var pipe: Pipe?
    private var lastStartAttempt = Date.distantPast
    private var pendingWrites = 0
    private var droppingUntilKeyframe = false
    private var stopping = false
    private let maximumPendingWrites = 8

    init(rtspURL: String) {
        self.rtspURL = rtspURL
    }

    var isRunning: Bool {
        lock.lock(); defer { lock.unlock() }
        return process?.isRunning == true
    }

    func write(_ data: Data, keyframe: Bool) {
        let accepted = lock.withLock { () -> Bool in
            guard !stopping else { return false }
            if droppingUntilKeyframe {
                guard keyframe, pendingWrites < maximumPendingWrites else { return false }
                droppingUntilKeyframe = false
            }
            guard pendingWrites < maximumPendingWrites else {
                droppingUntilKeyframe = true
                return false
            }
            pendingWrites += 1
            return true
        }
        guard accepted else { return }
        writerQueue.async { [weak self] in
            guard let self else { return }
            defer { self.lock.withLock { self.pendingWrites -= 1 } }
            self.lock.withLock {
                guard !self.stopping else { return }
                self.ensureRunningLocked()
                guard let handle = self.pipe?.fileHandleForWriting else { return }
                do {
                    try handle.write(contentsOf: data)
                } catch {
                    self.process?.terminate()
                    self.process = nil
                    self.pipe = nil
                    self.droppingUntilKeyframe = true
                }
            }
        }
    }

    func stop() {
        lock.withLock { stopping = true }
        writerQueue.sync {}
        lock.withLock {
            try? pipe?.fileHandleForWriting.close()
            process?.terminate()
            process = nil
            pipe = nil
        }
    }

    private func ensureRunningLocked() {
        if process?.isRunning == true { return }
        guard Date().timeIntervalSince(lastStartAttempt) >= 5 else { return }
        lastStartAttempt = Date()
        let candidates = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]
        guard let executable = candidates.first(where: FileManager.default.isExecutableFile) else { return }
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = Self.ffmpegArguments(rtspURL: rtspURL)
        process.standardInput = pipe
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.standardError
        do {
            try process.run()
            self.process = process
            self.pipe = pipe
        } catch {
            FileHandle.standardError.write(Data("RTSP muxer start failed: \(error)\n".utf8))
        }
    }

    static func ffmpegArguments(rtspURL: String) -> [String] {
        [
            "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-use_wallclock_as_timestamps", "1", "-fflags", "+genpts", "-f", "h264",
            "-i", "-", "-c:v", "copy", "-fps_mode", "passthrough",
            "-rtsp_transport", "tcp", "-f", "rtsp", rtspURL,
        ]
    }
}

private let h264OutputCallback: VTCompressionOutputCallback = { refcon, _, status, _, sampleBuffer in
    guard status == noErr, let refcon, let sampleBuffer else { return }
    let encoder = Unmanaged<H264HardwareEncoder>.fromOpaque(refcon).takeUnretainedValue()
    encoder.handle(sampleBuffer)
}

final class H264HardwareEncoder: @unchecked Sendable {
    private static let annexBStartCode = Data([0, 0, 0, 1])
    private let publisher: RTSPPublisher
    private let width: Int
    private let height: Int
    private let targetFPS: Double
    private let bitrate: Int
    private var session: VTCompressionSession?

    init(width: Int, height: Int, targetFPS: Double, bitrate: Int, publisher: RTSPPublisher) throws {
        self.width = width
        self.height = height
        self.targetFPS = targetFPS
        self.bitrate = bitrate
        self.publisher = publisher
        try createSession()
    }

    deinit {
        if let session {
            VTCompressionSessionCompleteFrames(session, untilPresentationTimeStamp: .invalid)
            VTCompressionSessionInvalidate(session)
        }
    }

    @discardableResult
    func encode(_ pixelBuffer: CVPixelBuffer, presentationTime: CMTime) -> Bool {
        guard let session else { return false }
        var flags = VTEncodeInfoFlags()
        let status = VTCompressionSessionEncodeFrame(
            session,
            imageBuffer: pixelBuffer,
            presentationTimeStamp: presentationTime,
            duration: .invalid,
            frameProperties: nil,
            sourceFrameRefcon: nil,
            infoFlagsOut: &flags
        )
        if status != noErr {
            FileHandle.standardError.write(Data("H.264 encode failed: \(status)\n".utf8))
            return false
        }
        return true
    }

    fileprivate func handle(_ sampleBuffer: CMSampleBuffer) {
        guard CMSampleBufferDataIsReady(sampleBuffer), let block = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
        let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[CFString: Any]]
        let notSync = attachments?.first?[kCMSampleAttachmentKey_NotSync] as? Bool ?? false
        let keyframe = !notSync
        var output = Data()
        output.reserveCapacity(CMBlockBufferGetDataLength(block) + 256)

        if keyframe, let format = CMSampleBufferGetFormatDescription(sampleBuffer) {
            for index in 0..<2 {
                var pointer: UnsafePointer<UInt8>?
                var size = 0
                var count = 0
                let status = CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                    format,
                    parameterSetIndex: index,
                    parameterSetPointerOut: &pointer,
                    parameterSetSizeOut: &size,
                    parameterSetCountOut: &count,
                    nalUnitHeaderLengthOut: nil
                )
                if status == noErr, let pointer {
                    output.append(Self.annexBStartCode)
                    output.append(pointer, count: size)
                }
            }
        }

        var totalLength = 0
        var dataPointer: UnsafeMutablePointer<Int8>?
        guard CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &totalLength, dataPointerOut: &dataPointer) == kCMBlockBufferNoErr,
              let dataPointer else { return }
        let bytes = UnsafeRawPointer(dataPointer).assumingMemoryBound(to: UInt8.self)
        var offset = 0
        while offset + 4 <= totalLength {
            let length = Int(bytes[offset]) << 24 | Int(bytes[offset + 1]) << 16 | Int(bytes[offset + 2]) << 8 | Int(bytes[offset + 3])
            offset += 4
            guard length > 0, offset + length <= totalLength else { break }
            output.append(Self.annexBStartCode)
            output.append(bytes + offset, count: length)
            offset += length
        }
        if !output.isEmpty { publisher.write(output, keyframe: keyframe) }
    }

    private func createSession() throws {
        let specification: [CFString: Any] = [
            kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder: true,
        ]
        let attributes: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey: width,
            kCVPixelBufferHeightKey: height,
            kCVPixelBufferMetalCompatibilityKey: true,
        ]
        var created: VTCompressionSession?
        let status = VTCompressionSessionCreate(
            allocator: nil,
            width: Int32(width),
            height: Int32(height),
            codecType: kCMVideoCodecType_H264,
            encoderSpecification: specification as CFDictionary,
            imageBufferAttributes: attributes as CFDictionary,
            compressedDataAllocator: nil,
            outputCallback: h264OutputCallback,
            refcon: Unmanaged.passUnretained(self).toOpaque(),
            compressionSessionOut: &created
        )
        guard status == noErr, let created else { throw EncoderError.session(status) }
        session = created
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_RealTime, value: kCFBooleanTrue)
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_ProfileLevel, value: kVTProfileLevel_H264_Baseline_AutoLevel)
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_AllowFrameReordering, value: kCFBooleanFalse)
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_AverageBitRate, value: bitrate as CFNumber)
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_MaxKeyFrameInterval, value: Int(targetFPS.rounded()) as CFNumber)
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration, value: 1 as CFNumber)
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_ExpectedFrameRate, value: Int(targetFPS.rounded()) as CFNumber)
        guard VTCompressionSessionPrepareToEncodeFrames(created) == noErr else { throw EncoderError.prepare }
    }
}

enum EncoderError: Error {
    case cannotAddWriterInput
    case writerStart
    case session(OSStatus)
    case prepare
}
