import CoreImage
import Foundation
import Vision

struct FaceConfiguration: Sendable, Equatable {
    var enabled: Bool
    var galleryDirectory: URL
    var matchThreshold: Double
    var matchMargin: Double
    var minHits: Int
    var minSize: Double
    var minQuality: Double
    var maxIdentities: Int
    var maxExemplars: Int

    static let featurePrintEmbedder = "vision-featureprint-v1"

    static func load(environment: [String: String] = ProcessInfo.processInfo.environment) -> Self {
        let enabled = (environment["CCTV_FACE_RECOGNITION"] ?? "1")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        let gallery = environment["CCTV_FACE_GALLERY_DIR"]
            ?? "~/.local/state/cctv/faces"
        return Self(
            enabled: enabled != "0" && enabled != "false" && enabled != "off",
            galleryDirectory: URL(
                fileURLWithPath: (gallery as NSString).expandingTildeInPath,
                isDirectory: true
            ),
            matchThreshold: Double(environment["CCTV_FACE_MATCH_THRESHOLD"] ?? "0.52") ?? 0.52,
            matchMargin: Double(environment["CCTV_FACE_MATCH_MARGIN"] ?? "0.04") ?? 0.04,
            minHits: max(1, Int(environment["CCTV_FACE_MIN_HITS"] ?? "2") ?? 2),
            minSize: Double(environment["CCTV_FACE_MIN_SIZE"] ?? "24") ?? 24,
            minQuality: Double(environment["CCTV_FACE_MIN_QUALITY"] ?? "0.05") ?? 0.05,
            maxIdentities: max(1, Int(environment["CCTV_FACE_MAX_IDENTITIES"] ?? "32") ?? 32),
            maxExemplars: max(1, Int(environment["CCTV_FACE_MAX_EXEMPLARS"] ?? "8") ?? 8)
        )
    }
}

extension SemanticLabel {
    var isAutoIdentity: Bool {
        guard name.count >= 2, name.first == "p" else { return false }
        return name.dropFirst().allSatisfy(\.isNumber)
    }

    static func identity(_ id: Int, confidence: Double) -> SemanticLabel {
        SemanticLabel(name: "p\(id)", confidence: confidence)
    }
}

enum FaceObservationOutcome: Equatable {
    case ignored
    case pending
    case matched(id: Int, confidence: Double, emit: Bool, recordExemplar: Bool)
    case enrolled(id: Int, confidence: Double, embedding: [Double])
    case rejectedAtCapacity
}

struct FaceIdentityRecord: Codable, Equatable, Sendable {
    var id: Int
    var embeddings: [[Double]]
    var qualities: [Double]
}

struct FaceGalleryFile: Codable, Equatable, Sendable {
    var version: Int
    var embedder: String
    var nextID: Int
    var identities: [FaceIdentityRecord]

    enum CodingKeys: String, CodingKey {
        case version, embedder, identities
        case nextID = "next_id"
    }
}

struct FaceDecisionEngine: Equatable {
    var configuration: FaceConfiguration
    var embedderVersion: String
    var identities: [FaceIdentityRecord]
    var nextID: Int
    var unknownEmbeddings: [[Double]] = []
    var unknownQualities: [Double] = []
    var hits: [Int: Int] = [:]
    var emitted: Set<Int> = []
    var lastObservation: Date?

    init(
        configuration: FaceConfiguration,
        embedderVersion: String = FaceConfiguration.featurePrintEmbedder,
        identities: [FaceIdentityRecord] = [],
        nextID: Int = 1
    ) {
        self.configuration = configuration
        self.embedderVersion = embedderVersion
        self.identities = identities
        self.nextID = max(nextID, (identities.map(\.id).max() ?? 0) + 1)
    }

    mutating func observe(
        embedding: [Double],
        quality: Double,
        at now: Date
    ) -> FaceObservationOutcome {
        if let lastObservation, now.timeIntervalSince(lastObservation) > 5 {
            unknownEmbeddings.removeAll(keepingCapacity: true)
            unknownQualities.removeAll(keepingCapacity: true)
        }
        lastObservation = now
        let normalized = FaceMath.l2Normalize(embedding)
        guard !normalized.isEmpty else { return .ignored }

        if let (identityID, best, second) = bestMatch(for: normalized) {
            let marginOK = best - second >= configuration.matchMargin
            if best >= configuration.matchThreshold && marginOK {
                unknownEmbeddings.removeAll(keepingCapacity: true)
                unknownQualities.removeAll(keepingCapacity: true)
                let count = (hits[identityID] ?? 0) + 1
                hits[identityID] = count
                let shouldEmit = count >= configuration.minHits && !emitted.contains(identityID)
                if shouldEmit { emitted.insert(identityID) }
                if count >= configuration.minHits {
                    return .matched(
                        id: identityID,
                        confidence: best,
                        emit: shouldEmit,
                        recordExemplar: true
                    )
                }
                return .pending
            }
        }

        if !unknownEmbeddings.isEmpty,
           !FaceMath.agrees(normalized, with: unknownEmbeddings, threshold: configuration.matchThreshold) {
            unknownEmbeddings = [normalized]
            unknownQualities = [quality]
            return .pending
        }
        unknownEmbeddings.append(normalized)
        unknownQualities.append(quality)
        guard unknownEmbeddings.count >= configuration.minHits,
              FaceMath.agrees(among: unknownEmbeddings, threshold: configuration.matchThreshold)
        else {
            return .pending
        }
        guard identities.count < configuration.maxIdentities else {
            unknownEmbeddings.removeAll(keepingCapacity: true)
            unknownQualities.removeAll(keepingCapacity: true)
            return .rejectedAtCapacity
        }
        let newID = nextID
        nextID += 1
        let record = FaceIdentityRecord(
            id: newID,
            embeddings: Array(unknownEmbeddings.suffix(configuration.maxExemplars)),
            qualities: Array(unknownQualities.suffix(configuration.maxExemplars))
        )
        identities.append(record)
        hits[newID] = unknownEmbeddings.count
        emitted.insert(newID)
        unknownEmbeddings.removeAll(keepingCapacity: true)
        unknownQualities.removeAll(keepingCapacity: true)
        return .enrolled(id: newID, confidence: 1, embedding: record.embeddings.last ?? normalized)
    }

    mutating func recordExemplar(id: Int, embedding: [Double], quality: Double) {
        guard let index = identities.firstIndex(where: { $0.id == id }) else { return }
        let normalized = FaceMath.l2Normalize(embedding)
        guard !normalized.isEmpty else { return }
        var record = identities[index]
        if record.embeddings.count < configuration.maxExemplars {
            record.embeddings.append(normalized)
            record.qualities.append(quality)
        } else if let worst = record.qualities.enumerated().min(by: { $0.element < $1.element }),
                  quality > worst.element {
            record.embeddings[worst.offset] = normalized
            record.qualities[worst.offset] = quality
        }
        identities[index] = record
    }

    mutating func endEpisode() {
        unknownEmbeddings.removeAll(keepingCapacity: true)
        unknownQualities.removeAll(keepingCapacity: true)
        hits.removeAll(keepingCapacity: true)
        emitted.removeAll(keepingCapacity: true)
        lastObservation = nil
    }

    func snapshot() -> FaceGalleryFile {
        FaceGalleryFile(
            version: 1,
            embedder: embedderVersion,
            nextID: nextID,
            identities: identities
        )
    }

    private func bestMatch(for embedding: [Double]) -> (Int, Double, Double)? {
        var bestID: Int?
        var best = -1.0
        var second = -1.0
        for identity in identities {
            let score = identity.embeddings
                .map { FaceMath.cosine(embedding, $0) }
                .max() ?? -1
            if score > best {
                second = best
                best = score
                bestID = identity.id
            } else if score > second {
                second = score
            }
        }
        guard let bestID else { return nil }
        return (bestID, best, second)
    }
}

enum FaceMath {
    static func cosine(_ lhs: [Double], _ rhs: [Double]) -> Double {
        guard lhs.count == rhs.count, !lhs.isEmpty else { return -1 }
        var dot = 0.0
        var leftNorm = 0.0
        var rightNorm = 0.0
        for index in lhs.indices {
            dot += lhs[index] * rhs[index]
            leftNorm += lhs[index] * lhs[index]
            rightNorm += rhs[index] * rhs[index]
        }
        let denominator = leftNorm.squareRoot() * rightNorm.squareRoot()
        guard denominator > 0 else { return -1 }
        return dot / denominator
    }

    static func l2Normalize(_ values: [Double]) -> [Double] {
        let norm = values.reduce(0) { $0 + $1 * $1 }.squareRoot()
        guard norm > 0 else { return [] }
        return values.map { $0 / norm }
    }

    static func agrees(_ candidate: [Double], with others: [[Double]], threshold: Double) -> Bool {
        others.allSatisfy { cosine(candidate, $0) >= threshold }
    }

    static func agrees(among vectors: [[Double]], threshold: Double) -> Bool {
        guard vectors.count >= 2 else { return false }
        for index in 0..<(vectors.count - 1) {
            for other in (index + 1)..<vectors.count {
                if cosine(vectors[index], vectors[other]) < threshold {
                    return false
                }
            }
        }
        return true
    }
}

struct FacePipelineResult: Sendable {
    var labels: [SemanticLabel]
    var events: [WorkerEvent]
}

final class FaceRecognizer: @unchecked Sendable {
    private let configuration: FaceConfiguration
    private let context: CIContext
    private let embedder: any FaceEmbedder
    private let lock = NSLock()
    private var engine: FaceDecisionEngine
    private var dirty = false

    init(configuration: FaceConfiguration, context: CIContext, embedder: (any FaceEmbedder)? = nil) {
        self.configuration = configuration
        self.context = context
        let resolved = embedder ?? VisionFeaturePrintEmbedder()
        self.embedder = resolved
        let loaded = FaceGalleryStore.load(
            directory: configuration.galleryDirectory,
            expectedEmbedder: resolved.version
        )
        self.engine = FaceDecisionEngine(
            configuration: configuration,
            embedderVersion: resolved.version,
            identities: loaded?.identities ?? [],
            nextID: loaded?.nextID ?? 1
        )
        FileHandle.standardError.write(
            Data(
                "[face] ready identities=\(loaded?.identities.count ?? 0) minSize=\(Int(configuration.minSize)) minQuality=\(configuration.minQuality) minHits=\(configuration.minHits) threshold=\(configuration.matchThreshold)\n".utf8
            )
        )
    }

    static func mergeDetections(_ lhs: [VNFaceObservation], _ rhs: [VNFaceObservation]) -> [VNFaceObservation] {
        var merged = lhs
        for face in rhs {
            if let index = merged.firstIndex(where: { $0.boundingBox.intersects(face.boundingBox) }) {
                let existingQuality = merged[index].faceCaptureQuality ?? 0
                if (face.faceCaptureQuality ?? 0) > existingQuality {
                    merged[index] = face
                }
            } else {
                merged.append(face)
            }
        }
        return merged
    }

    func observe(image: CIImage, faces: [VNFaceObservation], now: Date) -> FacePipelineResult {
        guard configuration.enabled else { return FacePipelineResult(labels: [], events: []) }
        var rejected: [String] = []
        let usable = faces
            .compactMap { face -> (CGRect, Double)? in
                let rect = Self.pixelRect(for: face, in: image)
                let side = min(rect.width, rect.height)
                if side < configuration.minSize {
                    rejected.append(String(format: "small %.0fx%.0f", rect.width, rect.height))
                    return nil
                }
                if let reported = face.faceCaptureQuality.map(Double.init),
                   reported < configuration.minQuality {
                    rejected.append(String(format: "quality %.2f", reported))
                    return nil
                }
                return (rect, Double(face.faceCaptureQuality ?? 1))
            }
            .sorted { min($0.0.width, $0.0.height) > min($1.0.width, $1.0.height) }
            .prefix(2)

        if !faces.isEmpty || !usable.isEmpty {
            let detail = rejected.isEmpty ? "ok" : rejected.joined(separator: ",")
            FileHandle.standardError.write(
                Data("[face] detected=\(faces.count) usable=\(usable.count) \(detail)\n".utf8)
            )
        }

        var labels: [SemanticLabel] = []
        var events: [WorkerEvent] = []
        for (rect, quality) in usable {
            let cropped = image.cropped(to: rect)
            guard let embedding = embedder.embed(face: cropped) else {
                FileHandle.standardError.write(
                    Data(String(format: "[face] embed failed size=%.0fx%.0f\n", rect.width, rect.height).utf8)
                )
                continue
            }
            let outcome = lock.withLock { engine.observe(embedding: embedding, quality: quality, at: now) }
            switch outcome {
            case .ignored:
                continue
            case .pending:
                FileHandle.standardError.write(Data("[face] pending quality=\(String(format: "%.2f", quality))\n".utf8))
                continue
            case let .matched(id, confidence, emit, recordExemplar):
                labels.append(.identity(id, confidence: confidence))
                if emit {
                    FileHandle.standardError.write(
                        Data(String(format: "[face] matched p%d conf=%.2f\n", id, confidence).utf8)
                    )
                }
                if recordExemplar {
                    lock.withLock {
                        engine.recordExemplar(id: id, embedding: embedding, quality: quality)
                        dirty = true
                    }
                }
                if emit {
                    events.append(
                        WorkerEvent(
                            type: "face.matched",
                            payload: .faceMatched(id: id, confidence: confidence)
                        )
                    )
                }
            case let .enrolled(id, confidence, enrolledEmbedding):
                labels.append(.identity(id, confidence: confidence))
                FileHandle.standardError.write(Data("[face] enrolled p\(id)\n".utf8))
                let cropPath = writeCrop(cropped, identityID: id)
                lock.withLock { dirty = true }
                events.append(
                    WorkerEvent(
                        type: "face.enrolled",
                        payload: .faceEnrolled(
                            id: id,
                            confidence: confidence,
                            quality: quality,
                            cropPath: cropPath,
                            embedding: enrolledEmbedding,
                            embedder: embedder.version
                        )
                    )
                )
            case .rejectedAtCapacity:
                FileHandle.standardError.write(
                    Data("Face gallery at capacity (\(configuration.maxIdentities)); skipping new identity.\n".utf8)
                )
            }
        }
        persistIfNeeded()
        return FacePipelineResult(
            labels: labels.sorted { $0.confidence > $1.confidence },
            events: events
        )
    }

    func endEpisode() {
        lock.withLock {
            engine.endEpisode()
        }
        persistIfNeeded()
    }

    private func persistIfNeeded() {
        let snapshot: FaceGalleryFile? = lock.withLock {
            guard dirty else { return nil }
            dirty = false
            return engine.snapshot()
        }
        guard let snapshot else { return }
        FaceGalleryStore.save(snapshot, to: configuration.galleryDirectory)
    }

    private func writeCrop(_ image: CIImage, identityID: Int) -> String? {
        let directory = configuration.galleryDirectory
            .appendingPathComponent("exemplars", isDirectory: true)
            .appendingPathComponent("p\(identityID)", isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        } catch {
            return nil
        }
        let url = directory.appendingPathComponent("\(Int(Date().timeIntervalSince1970 * 1000)).jpg")
        let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB()
        guard let data = context.jpegRepresentation(of: image, colorSpace: colorSpace, options: [:]) else {
            return nil
        }
        do {
            try data.write(to: url, options: .atomic)
            return url.path
        } catch {
            return nil
        }
    }

    static func pixelRect(for face: VNFaceObservation, in image: CIImage) -> CGRect {
        let extent = image.extent
        let box = face.boundingBox
        let padding = 0.18
        let padded = box.insetBy(dx: -box.width * padding, dy: -box.height * padding)
        let rect = CGRect(
            x: extent.minX + padded.origin.x * extent.width,
            y: extent.minY + padded.origin.y * extent.height,
            width: padded.width * extent.width,
            height: padded.height * extent.height
        )
        return rect.intersection(extent)
    }
}

enum FaceGalleryStore {
    static func load(directory: URL, expectedEmbedder: String) -> FaceGalleryFile? {
        let url = directory.appendingPathComponent("gallery.json")
        guard let data = try? Data(contentsOf: url) else { return nil }
        do {
            let decoded = try JSONDecoder().decode(FaceGalleryFile.self, from: data)
            if decoded.embedder != expectedEmbedder {
                FileHandle.standardError.write(
                    Data("Face gallery embedder \(decoded.embedder) != \(expectedEmbedder); starting empty.\n".utf8)
                )
                return nil
            }
            return decoded
        } catch {
            FileHandle.standardError.write(Data("Face gallery load failed: \(error)\n".utf8))
            return nil
        }
    }

    static func save(_ gallery: FaceGalleryFile, to directory: URL) {
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            let data = try encoder.encode(gallery)
            let url = directory.appendingPathComponent("gallery.json")
            let temporary = url.appendingPathExtension("tmp")
            try data.write(to: temporary, options: .atomic)
            if FileManager.default.fileExists(atPath: url.path) {
                _ = try FileManager.default.replaceItemAt(url, withItemAt: temporary)
            } else {
                try FileManager.default.moveItem(at: temporary, to: url)
            }
        } catch {
            FileHandle.standardError.write(Data("Face gallery save failed: \(error)\n".utf8))
        }
    }
}

protocol FaceEmbedder: Sendable {
    var version: String { get }
    func embed(face: CIImage) -> [Double]?
}

struct VisionFeaturePrintEmbedder: FaceEmbedder {
    let version = FaceConfiguration.featurePrintEmbedder

    func embed(face: CIImage) -> [Double]? {
        do {
            return try runSync {
                let request = GenerateImageFeaturePrintRequest()
                let observation = try await request.perform(on: face)
                return Self.vector(from: observation)
            }
        } catch {
            FileHandle.standardError.write(Data("Face embedding failed: \(error)\n".utf8))
            return nil
        }
    }

    static func vector(from observation: FeaturePrintObservation) -> [Double]? {
        let data = observation.data
        let count = observation.elementCount
        guard count > 0 else { return nil }
        if data.count == count * MemoryLayout<Float>.size {
            return data.withUnsafeBytes { buffer in
                Array(buffer.bindMemory(to: Float.self)).map(Double.init)
            }
        }
        if data.count == count * MemoryLayout<Double>.size {
            return data.withUnsafeBytes { buffer in
                Array(buffer.bindMemory(to: Double.self))
            }
        }
        return nil
    }

    private func runSync<T: Sendable>(_ work: @escaping @Sendable () async throws -> T) throws -> T {
        let box = FaceSyncBox<T>()
        let semaphore = DispatchSemaphore(value: 0)
        Task.detached(priority: .utility) {
            do {
                box.result = .success(try await work())
            } catch {
                box.result = .failure(error)
            }
            semaphore.signal()
        }
        semaphore.wait()
        return try box.result!.get()
    }
}

private final class FaceSyncBox<T: Sendable>: @unchecked Sendable {
    var result: Result<T, any Error>?
}
