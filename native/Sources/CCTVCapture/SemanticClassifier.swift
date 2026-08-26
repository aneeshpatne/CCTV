@preconcurrency import CoreML
@preconcurrency import CoreImage
@preconcurrency import Vision
import CoreVideo
import Foundation

struct SemanticOutcome: Sendable {
    var labels: [SemanticLabel]
    var faceEvents: [WorkerEvent]
}

final class SemanticClassifier: @unchecked Sendable {
    private let queue = DispatchQueue(label: "cctv.semantic", qos: .utility)
    private let lock = NSLock()
    private var busy = false
    private var lastRun = Date.distantPast
    private var cached: [SemanticLabel] = []
    private let faces: FaceRecognizer?

    init(faces: FaceRecognizer? = nil) {
        self.faces = faces
    }

    /// Return the latest labels immediately and schedule at most one candidate inference.
    /// Completion runs off the capture path so Vision cannot reduce camera throughput.
    func labels(
        for image: CIImage,
        candidate: Bool,
        now: Date = Date(),
        completion: @escaping @Sendable (SemanticOutcome) -> Void
    ) -> [SemanticLabel] {
        guard candidate else { return lock.withLock { cached } }
        let (shouldRun, existing) = lock.withLock {
            let shouldRun = !busy && now.timeIntervalSince(lastRun) >= 0.5
            if shouldRun {
                busy = true
                lastRun = now
            }
            return (shouldRun, cached)
        }
        guard shouldRun else { return existing }

        queue.async { [weak self] in
            guard let self else { return }
            let outcome = self.perform(image, now: now)
            self.lock.withLock {
                self.cached = outcome.labels
                self.busy = false
            }
            completion(outcome)
        }
        return existing
    }

    func endFaceEpisode() {
        faces?.endEpisode()
    }

    private func perform(_ image: CIImage, now: Date) -> SemanticOutcome {
        var result: [SemanticLabel] = []
        var faceEvents: [WorkerEvent] = []
        let human = VNDetectHumanRectanglesRequest()
        human.upperBodyOnly = false
        let classify = VNClassifyImageRequest()
        let faceRects = VNDetectFaceRectanglesRequest()
        let faceQuality = VNDetectFaceCaptureQualityRequest()
        let handler = VNImageRequestHandler(ciImage: image, orientation: .up)
        do {
            try handler.perform([human, classify, faceRects, faceQuality])
            if let people = human.results, let best = people.max(by: { $0.confidence < $1.confidence }) {
                result.append(SemanticLabel(name: "person", confidence: Double(best.confidence)))
            }
            let classResults = classify.results ?? []
            for observation in classResults.prefix(12) where observation.confidence >= 0.2 {
                let lower = observation.identifier.lowercased()
                let mapped: String?
                if ["animal", "dog", "cat", "bird", "cow", "horse"].contains(where: lower.contains) {
                    mapped = "animal"
                } else {
                    mapped = nil
                }
                if let mapped {
                    let confidence = Double(observation.confidence)
                    if let index = result.firstIndex(where: { $0.name == mapped }) {
                        if confidence > result[index].confidence {
                            result[index] = SemanticLabel(name: mapped, confidence: confidence)
                        }
                    } else {
                        result.append(SemanticLabel(name: mapped, confidence: confidence))
                    }
                }
            }
            if let faces {
                let recognized = faces.observe(
                    image: image,
                    faces: FaceRecognizer.mergeDetections(
                        faceRects.results ?? [],
                        faceQuality.results ?? []
                    ),
                    now: now
                )
                result.append(contentsOf: recognized.labels)
                faceEvents = recognized.events
            }
        } catch {
            FileHandle.standardError.write(Data("Vision classification failed: \(error)\n".utf8))
        }
        let identities = result.filter(\.isAutoIdentity).sorted { $0.confidence > $1.confidence }
        let others = result.filter { !$0.isAutoIdentity }.sorted { $0.confidence > $1.confidence }
        return SemanticOutcome(labels: identities + others, faceEvents: faceEvents)
    }
}
