@preconcurrency import AppKit
@preconcurrency import CoreImage
import CoreVideo
import Foundation
import Metal

struct CameraSettings: Sendable, Equatable {
    var framesize: Int?
    var xclk: Int?
    var autoExposure: Bool?
    var shutterLines: Int?
    var autoGain: Bool?
    var gainX16: Int?
    var gainRegister: Int?
    var autoWhiteBalance: Bool?
    var red: Int?
    var green: Int?
    var blue: Int?
    var saturationU: Int?
    var saturationV: Int?
    var cachedForRecovery: Bool?
    var wbControllerState: String? = nil

    var imageSummary: String? {
        var values: [String] = []
        if let xclk { values.append("XCLK \(xclk)") }
        if let autoExposure { values.append(autoExposure ? "AE AUTO" : "AE MANUAL") }
        if let shutterLines { values.append("\(shutterLines)L") }
        if let autoGain { values.append(autoGain ? "AGC AUTO" : "AGC MANUAL") }
        if let gainX16 { values.append("GAIN \(gainX16)/16") }
        if let gainRegister { values.append("REG \(gainRegister)") }
        if let autoWhiteBalance { values.append(autoWhiteBalance ? "AWB AUTO" : "AWB MANUAL") }
        if let red, let green, let blue { values.append("WB \(red)/\(green)/\(blue)") }
        if let wbControllerState { values.append("WBCTRL \(wbControllerState.uppercased())") }
        if let saturationU, let saturationV { values.append("SAT \(saturationU)/\(saturationV)") }
        if cachedForRecovery == false { values.append("NOT CACHED") }
        return values.isEmpty ? nil : values.joined(separator: " · ")
    }
}

struct HUDStatus: Sendable {
    var fps: Double = 0
    var rssi: Int?
    var temperature: Double?
    var sceneBrightness: Double?
    var redOverGreen: Double? = nil
    var blueOverGreen: Double? = nil
    var motion = false
    var floodlightOn = false
    var labels: [SemanticLabel] = []
    var motionBox: NormalizedRect?
    var message: String?
    var cameraSettings = CameraSettings()
}

actor CameraTelemetry {
    private(set) var rssi: Int?
    private(set) var temperature: Double?
    private(set) var cameraSettings = CameraSettings()
    private let baseURL: URL
    private let floodlightStatePath: String

    init(baseURL: URL) {
        self.baseURL = baseURL
        self.floodlightStatePath = (
            ProcessInfo.processInfo.environment["CCTV_FLOODLIGHT_STATE_PATH"]
                ?? "~/.local/state/cctv/floodlight.json"
        ) as NSString as String
    }

    func snapshot() -> (Int?, Double?, CameraSettings, Bool) {
        (rssi, temperature, cameraSettings, readFloodlightState())
    }

    private func readFloodlightState() -> Bool {
        let path = (floodlightStatePath as NSString).expandingTildeInPath
        guard let data = FileManager.default.contents(atPath: path),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["enabled"] as? Bool == true else {
            return false
        }
        return object["on"] as? Bool == true
    }

    func pollForever() async {
        while !Task.isCancelled {
            async let nextRSSI = fetchNumber(path: "/rssi", key: "rssi")
            async let nextTemperature = fetchNumber(path: "/syshealth", key: "socTempC")
            async let nextCameraSettings = fetchCameraSettings()
            let (rssiValue, temperatureValue, settingsValue) = await (
                nextRSSI, nextTemperature, nextCameraSettings
            )
            if let rssiValue { rssi = Int(rssiValue) }
            if let temperatureValue { temperature = temperatureValue }
            if let settingsValue { cameraSettings = settingsValue }
            try? await Task.sleep(for: .seconds(10))
        }
    }

    private func fetchCameraSettings() async -> CameraSettings? {
        // JSONSerialization produces [String: Any], which is not Sendable under
        // Swift 6; keep these small telemetry reads actor-isolated and sequential.
        let legacyValue = await fetchObject(path: "/status")
        let currentValue = await fetchObject(path: "/status-v2")
        let imageValue = await fetchObject(path: "/image-control")
        let merged = (legacyValue ?? [:]).merging(currentValue ?? [:]) { _, current in current }
        guard !merged.isEmpty || imageValue != nil else { return nil }
        let exposure = imageValue?["exposure"] as? [String: Any]
        let whiteBalance = imageValue?["whiteBalance"] as? [String: Any]
        let color = imageValue?["color"] as? [String: Any]
        let saturation = color?["saturation"] as? [String: Any]
        let wbControllerState = fetchWBControllerState()
        return CameraSettings(
            framesize: intValue(merged["framesize"]),
            xclk: intValue(merged["xclk"]),
            autoExposure: boolValue(exposure?["autoExposure"]),
            shutterLines: intValue(exposure?["shutterLines"]),
            autoGain: boolValue(exposure?["autoGain"]),
            gainX16: intValue(exposure?["gainX16"]),
            gainRegister: intValue(exposure?["gainRegister"]),
            autoWhiteBalance: boolValue(whiteBalance?["auto"]),
            red: intValue(whiteBalance?["red"]),
            green: intValue(whiteBalance?["green"]),
            blue: intValue(whiteBalance?["blue"]),
            saturationU: intValue(saturation?["u"]),
            saturationV: intValue(saturation?["v"]),
            cachedForRecovery: boolValue(imageValue?["cachedForRecovery"]),
            wbControllerState: wbControllerState
        )
    }

    private func fetchWBControllerState() -> String? {
        let environment = ProcessInfo.processInfo.environment
        let configured = environment["CCTV_IMAGE_CONTROL_STATE_PATH"]
            ?? "~/.local/state/cctv/image-control.json"
        let path = (configured as NSString).expandingTildeInPath
        guard let data = FileManager.default.contents(atPath: path),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return object["controllerState"] as? String
    }

    private func intValue(_ value: Any?) -> Int? {
        if let number = value as? NSNumber { return number.intValue }
        if let string = value as? String { return Int(string) }
        return nil
    }

    private func boolValue(_ value: Any?) -> Bool? {
        if let value = value as? Bool { return value }
        if let number = value as? NSNumber { return number.boolValue }
        return nil
    }

    private func fetchObject(path: String) async -> [String: Any]? {
        guard let url = URL(string: path, relativeTo: baseURL) else { return nil }
        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
            return try JSONSerialization.jsonObject(with: data) as? [String: Any]
        } catch {
            return nil
        }
    }

    private func fetchNumber(path: String, key: String) async -> Double? {
        guard let url = URL(string: path, relativeTo: baseURL) else { return nil }
        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            guard (response as? HTTPURLResponse)?.statusCode == 200,
                  let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
            if let number = object[key] as? NSNumber { return number.doubleValue }
            return nil
        } catch {
            return nil
        }
    }
}

final class HUDRenderer: @unchecked Sendable {
    let context: CIContext
    private var pool: CVPixelBufferPool?
    private var poolSize = CGSize.zero
    private var textCache: [String: CIImage] = [:]
    private var textCacheUse: [String: UInt64] = [:]
    private var textCacheClock: UInt64 = 0
    private var hideUntil: [String: Date] = [:]
    private let lock = NSLock()
    private let colorSpace = CGColorSpaceCreateDeviceRGB()

    init() {
        if let device = MTLCreateSystemDefaultDevice() {
            self.context = CIContext(mtlDevice: device, options: [.cacheIntermediates: false])
        } else {
            self.context = CIContext(options: [.useSoftwareRenderer: false, .cacheIntermediates: false])
        }
    }

    func decodeJPEG(_ data: Data) -> CIImage? {
        CIImage(data: data, options: [.applyOrientationProperty: true])
    }

    func renderNoSignal(status: HUDStatus, now: Date = Date()) -> CVPixelBuffer? {
        let extent = CGRect(x: 0, y: 0, width: 1024, height: 768)
        let background = CIImage(color: CIColor(red: 0.025, green: 0.03, blue: 0.04))
            .cropped(to: extent)
        return render(background, status: status, now: now)
    }

    func render(_ source: CIImage, status: HUDStatus, now: Date = Date()) -> CVPixelBuffer? {
        lock.lock()
        defer { lock.unlock() }
        let sourceExtent = source.extent.integral
        guard sourceExtent.width > 0, sourceExtent.height > 0,
              let output = makeBuffer(width: Int(sourceExtent.width), height: Int(sourceExtent.height)) else {
            return nil
        }

        let base = source.transformed(
            by: CGAffineTransform(translationX: -sourceExtent.minX, y: -sourceExtent.minY)
        )
        var composed = base
        let height = sourceExtent.height
        let width = sourceExtent.width
        let top = height - 48
        let panelHeight: CGFloat = 34
        let gap: CGFloat = 6

        if let box = status.motionBox, box.y < 0.10 {
            hideUntil["timestamp"] = now.addingTimeInterval(5)
        }

        var left: CGFloat = gap
        if shouldDraw("timestamp", now: now) {
            let timestamp = timestampFormatter.string(from: now)
            composed = panel(text: timestamp, x: left, y: top, width: 196, height: panelHeight, accent: nil, over: composed)
        }
        left += 202

        if status.motion {
            let strongest = status.labels.first?.name.uppercased()
            let label = strongest.map { "MOTION · \($0)" } ?? "MOTION"
            composed = panel(text: label, x: left, y: top, width: strongest == nil ? 86 : 150, height: panelHeight, accent: CIColor(red: 0.98, green: 0.74, blue: 0.02), over: composed)
            left += strongest == nil ? 92 : 156
        }

        if status.floodlightOn {
            composed = panel(
                text: "● LIGHT",
                x: left,
                y: top,
                width: 94,
                height: panelHeight,
                accent: CIColor(red: 0.98, green: 0.74, blue: 0.02),
                over: composed
            )
            left += 100
        }

        let sceneText = status.sceneBrightness.map { String(format: "SCENE %.0f%%", $0 * 100) } ?? "SCENE --"
        composed = panel(
            text: sceneText,
            x: left,
            y: top,
            width: 104,
            height: panelHeight,
            accent: statusColor(forSceneBrightness: status.sceneBrightness),
            over: composed
        )

        var right = width - gap
        let temperature = status.temperature.map { String(format: "%.1fC", $0) } ?? "--C"
        right -= 94
        composed = panel(text: temperature, x: right, y: top, width: 94, height: panelHeight, accent: statusColor(forTemperature: status.temperature), over: composed)
        right -= gap + 88
        composed = panel(text: String(format: "%.0f fps", status.fps), x: right, y: top, width: 88, height: panelHeight, accent: statusColor(forFPS: status.fps), over: composed)
        right -= gap + 104
        let rssi = status.rssi.map { "\($0)dBm" } ?? "--dBm"
        composed = panel(text: rssi, x: right, y: top, width: 104, height: panelHeight, accent: statusColor(forRSSI: status.rssi), over: composed)

        if let message = status.message {
            let messageImage = textImage(message, size: 22, color: .white)
            let origin = CGPoint(x: max(12, (width - messageImage.extent.width) / 2), y: height * 0.48)
            composed = messageImage.transformed(by: .init(translationX: origin.x, y: origin.y)).composited(over: composed)
        }

        context.render(
            composed,
            to: output,
            bounds: CGRect(x: 0, y: 0, width: width, height: height),
            colorSpace: colorSpace
        )
        return output
    }

    private func panel(text: String, x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat, accent: CIColor?, over background: CIImage) -> CIImage {
        var result = background
        let shadow = CIImage(color: CIColor(red: 0, green: 0, blue: 0, alpha: 0.22))
            .cropped(to: CGRect(x: x, y: y - 2, width: width, height: height))
        result = shadow.composited(over: result)
        let surface = CIImage(color: CIColor(red: 0.12, green: 0.12, blue: 0.12, alpha: 0.92))
            .cropped(to: CGRect(x: x, y: y, width: width, height: height))
        result = surface.composited(over: result)
        if let accent {
            let strip = CIImage(color: accent).cropped(to: CGRect(x: x, y: y, width: 3, height: height))
            result = strip.composited(over: result)
        }
        let glyphs = textImage(text, size: 14, color: .white)
        let ty = y + (height - glyphs.extent.height) / 2
        return glyphs.transformed(by: .init(translationX: x + 12 - glyphs.extent.minX, y: ty - glyphs.extent.minY)).composited(over: result)
    }

    private func textImage(_ text: String, size: CGFloat, color: NSColor) -> CIImage {
        let cacheKey = "\(Int(size))|\(text)"
        textCacheClock &+= 1
        if let cached = textCache[cacheKey] {
            textCacheUse[cacheKey] = textCacheClock
            return cached
        }
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: .medium),
            .foregroundColor: color,
        ]
        let attributed = NSAttributedString(string: text, attributes: attributes)
        let filter = CIFilter(name: "CIAttributedTextImageGenerator")!
        filter.setValue(attributed, forKey: "inputText")
        filter.setValue(1.0, forKey: "inputScaleFactor")
        let image = filter.outputImage ?? CIImage.empty()
        textCache[cacheKey] = image
        textCacheUse[cacheKey] = textCacheClock
        if textCache.count > 160 {
            let oldest = textCacheUse.min { $0.value < $1.value }?.key
            if let oldest, oldest != cacheKey {
                textCache.removeValue(forKey: oldest)
                textCacheUse.removeValue(forKey: oldest)
            }
        }
        return image
    }

    private func shouldDraw(_ key: String, now: Date) -> Bool {
        guard let until = hideUntil[key] else { return true }
        if now >= until { hideUntil.removeValue(forKey: key); return true }
        return false
    }

    private func makeBuffer(width: Int, height: Int) -> CVPixelBuffer? {
        let size = CGSize(width: width, height: height)
        if pool == nil || poolSize != size {
            let attributes: [CFString: Any] = [
                kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
                kCVPixelBufferWidthKey: width,
                kCVPixelBufferHeightKey: height,
                kCVPixelBufferMetalCompatibilityKey: true,
                kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary,
            ]
            var newPool: CVPixelBufferPool?
            guard CVPixelBufferPoolCreate(nil, nil, attributes as CFDictionary, &newPool) == kCVReturnSuccess else { return nil }
            pool = newPool
            poolSize = size
        }
        var output: CVPixelBuffer?
        guard let pool, CVPixelBufferPoolCreatePixelBuffer(nil, pool, &output) == kCVReturnSuccess else { return nil }
        return output
    }

    private func statusColor(forFPS fps: Double) -> CIColor {
        fps >= 7 ? CIColor(red: 0.50, green: 0.79, blue: 0.58) : fps >= 5 ? CIColor(red: 0.98, green: 0.74, blue: 0.02) : CIColor(red: 0.95, green: 0.55, blue: 0.51)
    }

    private func statusColor(forRSSI rssi: Int?) -> CIColor {
        guard let rssi else { return CIColor(red: 0.5, green: 0.5, blue: 0.5) }
        return rssi >= -70 ? CIColor(red: 0.50, green: 0.79, blue: 0.58) : rssi >= -80 ? CIColor(red: 0.98, green: 0.74, blue: 0.02) : CIColor(red: 0.95, green: 0.55, blue: 0.51)
    }

    private func statusColor(forTemperature temperature: Double?) -> CIColor {
        guard let temperature else { return CIColor(red: 0.5, green: 0.5, blue: 0.5) }
        return temperature < 70 ? CIColor(red: 0.50, green: 0.79, blue: 0.58) : temperature < 80 ? CIColor(red: 0.98, green: 0.74, blue: 0.02) : CIColor(red: 0.95, green: 0.55, blue: 0.51)
    }

    private func statusColor(forSceneBrightness brightness: Double?) -> CIColor {
        guard let brightness else { return CIColor(red: 0.5, green: 0.5, blue: 0.5) }
        return brightness <= 0.18
            ? CIColor(red: 0.95, green: 0.55, blue: 0.51)
            : brightness <= 0.30
            ? CIColor(red: 0.98, green: 0.74, blue: 0.02)
            : CIColor(red: 0.50, green: 0.79, blue: 0.58)
    }

    private let timestampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "Asia/Kolkata")
        formatter.dateFormat = "yyyy-MM-dd hh:mm:ss a"
        return formatter
    }()
}
