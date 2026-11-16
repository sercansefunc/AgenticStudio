import SwiftUI

@main
struct AgenticStudioApp: App {
    @StateObject private var state = AppState()
    var body: some Scene {
        WindowGroup("Agentic Studio") {
            ContentView()
                .environmentObject(state)
                .task { await state.bootstrap() }
        }
        .commands {
            CommandGroup(after: .appInfo) {
                Button("Tara") { Task { await state.scan() } }
                    .keyboardShortcut("r", modifiers: [.command, .shift])
                    .disabled(state.isRunning)
                Button("Yenile") { Task { await state.refresh() } }
                    .keyboardShortcut("r", modifiers: [.command])
            }
        }
    }
}
