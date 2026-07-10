@preconcurrency import CoreML
@preconcurrency import CoreImage
@preconcurrency import Vision
import CoreVideo
import Foundation

final class SemanticClassifier: @unchecked Sendable {
    private let queue = DispatchQueue(label: "cctv.semantic", qos: .utility)
    private let lock = NSLock()
    private var busy = false
    private var lastRun = Date.distantPast
    private var cached: [SemanticLabel] = []

    /// Return the latest labels immediately and schedule at most one candidate inference.
    /// Completion runs off the capture path so Vision cannot reduce camera throughput.
    func labels(
        for image: CIImage,
        candidate: Bool,
        now: Date = Date(),
        completion: @escaping @Sendable ([SemanticLabel]) -> Void
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
            let labels = self.perform(image)
            self.lock.withLock {
                self.cached = labels
                self.busy = false
            }
            completion(labels)
        }
        return existing
    }

    private func perform(_ image: CIImage) -> [SemanticLabel] {
        var result: [SemanticLabel] = []
        let human = VNDetectHumanRectanglesRequest()
        human.upperBodyOnly = false
        let classify = VNClassifyImageRequest()
        let handler = VNImageRequestHandler(ciImage: image, orientation: .up)
        do {
            try handler.perform([human, classify])
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
        } catch {
            FileHandle.standardError.write(Data("Vision classification failed: \(error)\n".utf8))
        }
        return result.sorted { $0.confidence > $1.confidence }
    }
}
