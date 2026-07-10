// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "CCTVNative",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "cctv-capture", targets: ["CCTVCapture"]),
    ],
    targets: [
        .executableTarget(
            name: "CCTVCapture",
            path: "Sources/CCTVCapture",
            swiftSettings: [
                .enableUpcomingFeature("ExistentialAny"),
            ]
        ),
        .testTarget(
            name: "CCTVCaptureTests",
            dependencies: ["CCTVCapture"],
            path: "Tests/CCTVCaptureTests"
        ),
    ]
)
