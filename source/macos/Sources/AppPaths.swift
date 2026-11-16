import AppKit
import Foundation

/// Runtime data lives outside the app bundle and the source tree.
struct AppPaths {
    let home: URL
    let reports: URL

    static func current() -> AppPaths {
        // AGENTICSTUDIO_HOME points a development build at a scratch data folder.
        let override = ProcessInfo.processInfo.environment["AGENTICSTUDIO_HOME"].map { URL(fileURLWithPath: $0, isDirectory: true) }
        let base = override ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("AgenticStudio", isDirectory: true)
        return AppPaths(home: base, reports: base.appendingPathComponent("reports", isDirectory: true))
    }

    func prepare() throws {
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
    }

    func report(_ day: String) -> URL { reports.appendingPathComponent("\(day).md") }

    func openHome() { NSWorkspace.shared.open(home) }
}
