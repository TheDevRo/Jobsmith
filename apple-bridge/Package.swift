// swift-tools-version:5.9
import PackageDescription

// Deployment target stays low (macOS 13) even though FoundationModels needs
// macOS 26: the framework is weak-linked and every call sits behind
// `#available(macOS 26.0, *)`, so the binary loads on older Macs and reports
// "unavailable" over HTTP instead of failing to launch.
let package = Package(
    name: "jobsmith-apple-ai",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "jobsmith-apple-ai", targets: ["JobsmithAppleAI"])
    ],
    targets: [
        .executableTarget(
            name: "JobsmithAppleAI",
            path: "Sources/JobsmithAppleAI"
        )
    ]
)
