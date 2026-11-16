import SwiftUI

struct ContentView: View {
    @EnvironmentObject var state: AppState
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack(alignment: .top) {
            NavigationSplitView {
                SidebarView()
                    .navigationSplitViewColumnWidth(min: 220, ideal: 235, max: 280)
            } detail: {
                Group {
                    switch state.section {
                    case .overview: OverviewView()
                    case .leads: LeadsView()
                    case .reports: ReportsView()
                    case .run: RunView()
                    case .settings: SettingsView()
                    }
                }
                .navigationTitle(state.section.rawValue)
            }
            .navigationSplitViewStyle(.balanced)
            // Toolbar holds only fixed-width items (short title, Tara); variable text lives in the content
            // so reopening the sidebar never squeezes the toolbar into its » overflow menu.
            .toolbar {
                ToolbarItem(placement: .primaryAction) { ScanButton() }
            }

            if let notice = state.notice {
                NoticeView(notice: notice, onDismiss: { state.dismissNotice() })
                    .onHover { state.noticeHovered = $0 }
                    .frame(maxWidth: 620)
                    .padding(.top, 12)
                    .transition(noticeTransition)
                    .zIndex(10)
            }
        }
        .frame(minWidth: 1000, minHeight: 640)
    }

    /// Enters and leaves through the top edge; the exit is quicker than the entrance.
    private var noticeTransition: AnyTransition {
        let path: AnyTransition = reduceMotion ? .opacity : .move(edge: .top).combined(with: .opacity)
        return .asymmetric(insertion: path, removal: path.animation(Motion.exit))
    }
}

struct ScanButton: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        if state.isRunning {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Button("Durdur") { state.stopScan() }
            }
        } else {
            Button { Task { await state.scan() } } label: {
                Label("Tara", systemImage: "arrow.clockwise")
            }
            .help("Tüm açık kaynakları şimdi tara (⇧⌘R)")
        }
    }
}
