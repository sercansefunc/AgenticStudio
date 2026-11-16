import Foundation

struct ProcessResult {
    let exitCode: Int32
    let output: String
}

enum BackendError: LocalizedError {
    case helperMissing
    case launchFailed(String)
    case invalidResponse(String)

    var errorDescription: String? {
        switch self {
        case .helperMissing:
            return "Agentic çekirdeği uygulama paketinde bulunamadı."
        case .launchFailed(let text), .invalidResponse(let text):
            return text
        }
    }
}

/// Runs the packaged `agenticstudio.desk` bridge; the last stdout line is its JSON reply.
final class BackendService: @unchecked Sendable {
    private let lock = NSLock()
    private var activeProcess: Process?
    private let home: URL

    init(home: URL) { self.home = home }

    private func helperURL() throws -> URL {
        if let override = ProcessInfo.processInfo.environment["AGENTIC_CORE"], !override.isEmpty {
            return URL(fileURLWithPath: override)
        }
        guard let resource = Bundle.main.resourceURL else { throw BackendError.helperMissing }
        let url = resource.appendingPathComponent("AgenticCore/agentic-core")
        guard FileManager.default.isExecutableFile(atPath: url.path) else { throw BackendError.helperMissing }
        return url
    }

    func stop() {
        lock.lock(); defer { lock.unlock() }
        activeProcess?.terminate()
    }

    func run(_ arguments: [String], environment extraEnvironment: [String: String] = [:],
             onLine: @escaping (String) -> Void = { _ in }) async throws -> ProcessResult {
        let executable = try helperURL()
        let fullArguments = ["--home", home.path] + arguments
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                let outputPipe = Pipe()
                process.executableURL = executable
                process.arguments = fullArguments
                var env = ProcessInfo.processInfo.environment
                env["PYTHONUNBUFFERED"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                for (key, value) in extraEnvironment { env[key] = value }
                process.environment = env
                process.standardOutput = outputPipe
                process.standardError = outputPipe

                self.lock.lock(); self.activeProcess = process; self.lock.unlock()
                var combined = Data()
                var pending = Data()
                let combineLock = NSLock()

                outputPipe.fileHandleForReading.readabilityHandler = { handle in
                    let data = handle.availableData
                    guard !data.isEmpty else { return }
                    combineLock.lock()
                    combined.append(data)
                    pending.append(data)
                    while let range = pending.firstRange(of: Data([0x0A])) {
                        let line = String(data: pending.subdata(in: 0..<range.lowerBound), encoding: .utf8) ?? ""
                        pending.removeSubrange(0...range.lowerBound)
                        DispatchQueue.main.async { onLine(line) }
                    }
                    combineLock.unlock()
                }

                process.terminationHandler = { proc in
                    outputPipe.fileHandleForReading.readabilityHandler = nil
                    let tail = outputPipe.fileHandleForReading.readDataToEndOfFile()
                    combineLock.lock()
                    combined.append(tail)
                    pending.append(tail)
                    if !pending.isEmpty, let line = String(data: pending, encoding: .utf8) {
                        for part in line.split(separator: "\n") {
                            let text = String(part)
                            DispatchQueue.main.async { onLine(text) }
                        }
                    }
                    let output = String(data: combined, encoding: .utf8) ?? ""
                    combineLock.unlock()
                    self.lock.lock(); self.activeProcess = nil; self.lock.unlock()
                    continuation.resume(returning: ProcessResult(exitCode: proc.terminationStatus, output: output))
                }

                do {
                    try process.run()
                } catch {
                    outputPipe.fileHandleForReading.readabilityHandler = nil
                    self.lock.lock(); self.activeProcess = nil; self.lock.unlock()
                    continuation.resume(throwing: BackendError.launchFailed(error.localizedDescription))
                }
            }
        }
    }

    /// Decodes the bridge's final JSON line; a reply with `ok: false` becomes an error.
    func decode<T: Decodable>(_ type: T.Type, from result: ProcessResult) throws -> T {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard let line = result.output.split(separator: "\n").last(where: { $0.hasPrefix("{") }),
              let data = String(line).data(using: .utf8) else {
            let tail = result.output.trimmingCharacters(in: .whitespacesAndNewlines).suffix(600)
            throw BackendError.invalidResponse(tail.isEmpty ? "Çekirdek yanıt vermedi (kod \(result.exitCode))." : String(tail))
        }
        let envelope = try decoder.decode(Envelope.self, from: data)
        guard envelope.ok else {
            throw BackendError.invalidResponse(envelope.error ?? "İşlem başarısız (kod \(result.exitCode)).")
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw BackendError.invalidResponse("Çekirdek yanıtı okunamadı: \(error.localizedDescription)")
        }
    }

    func call<T: Decodable>(_ type: T.Type, _ arguments: [String]) async throws -> T {
        try decode(type, from: try await run(arguments))
    }
}
