import Foundation

// jobsmith-apple-ai — a loopback OpenAI-compatible server in front of Apple's
// on-device foundation model, shipped as a Tauri sidecar. The Python backend
// spawns it, reads the port off stdout, and points a tier's base_url at it.
//
//   jobsmith-apple-ai [--port N]   (N omitted or 0 → the OS picks a free port)

func parsePort(_ arguments: [String]) -> UInt16? {
    guard let index = arguments.firstIndex(of: "--port") else { return 0 }
    guard index + 1 < arguments.count, let value = UInt16(arguments[index + 1]) else { return nil }
    return value
}

let arguments = Array(CommandLine.arguments.dropFirst())
guard let port = parsePort(arguments) else {
    FileHandle.standardError.write(Data("usage: jobsmith-apple-ai [--port N]\n".utf8))
    exit(2)
}

// Held for the process's lifetime: NWListener keeps itself alive once started,
// but the server's callbacks capture it weakly, so a scoped `let` would leave
// a bound socket nobody answers.
var server: HTTPServer?
do {
    server = try HTTPServer(port: port, handler: Router.handle)
    server?.start { boundPort in
        // The one line the spawner parses. Flush immediately — the parent
        // blocks on this, and a buffered pipe would look like a hang.
        print("{\"port\": \(boundPort)}")
        fflush(stdout)
    }
} catch {
    FileHandle.standardError.write(Data("jobsmith-apple-ai: cannot listen: \(error)\n".utf8))
    exit(1)
}

dispatchMain()
