// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "JobsmithKit",
    platforms: [.iOS(.v17)],
    products: [
        .library(name: "JobsmithKit", targets: ["JobsmithKit"])
    ],
    dependencies: [
        .package(url: "https://github.com/groue/GRDB.swift.git", from: "6.29.0"),
        .package(url: "https://github.com/scinfu/SwiftSoup.git", from: "2.7.0"),
        .package(url: "https://github.com/weichsel/ZIPFoundation.git", from: "0.9.19"),
    ],
    targets: [
        .target(
            name: "JobsmithKit",
            dependencies: [
                .product(name: "GRDB", package: "GRDB.swift"),
                "SwiftSoup",
                "ZIPFoundation",
            ]
        ),
        // No SPM test target on purpose: the Kit links UIKit-dependent code, so
        // `swift test` on macOS can't build it. Every test lives in
        // ios-standalone/KitTests and runs on the simulator via
        //   xcodebuild -scheme JobsmithKit -destination 'platform=iOS Simulator,…' test
    ]
)
