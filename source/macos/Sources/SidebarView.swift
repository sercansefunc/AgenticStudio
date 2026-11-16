import SwiftUI

struct SidebarView: View {
    @EnvironmentObject var state: AppState

    private var lastRunText: String {
        guard let run = state.status?.lastRun else { return "Henüz tarama yok" }
        if state.isRunning { return "Taranıyor…" }
        guard run.finishedAt != nil else { return "Yarım kalan tarama" }
        return "Son tarama \(ISODate.clock(run.finishedAt))"
    }

    private var lastRunColor: Color {
        guard let run = state.status?.lastRun, let code = run.exitCode else { return AppTheme.amber }
        if state.isRunning { return AppTheme.accent }
        return code == 0 ? AppTheme.green : (code == 75 ? AppTheme.amber : AppTheme.red)
    }

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 5) {
                Text("Agentic Studio").font(.headline)
                Text("fırsat masası").font(.callout).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16).padding(.top, 12).padding(.bottom, 8)

            List(selection: Binding<AppSection?>(
                get: { state.section },
                set: { if let value = $0 { state.section = value } }
            )) {
                ForEach(AppSection.allCases) { section in
                    HStack(spacing: 10) {
                        Image(systemName: section.icon).frame(width: 18)
                        Text(section.rawValue).lineLimit(1)
                        Spacer(minLength: 4)
                        if section == .leads {
                            Text("\(state.suggestedCount)").foregroundStyle(.secondary).font(.caption).monospacedDigit()
                                .contentTransition(.numericText())
                                .animation(Motion.standard, value: state.suggestedCount)
                        }
                        if section == .reports {
                            Text("\(state.status?.reports.count ?? 0)").foregroundStyle(.secondary).font(.caption).monospacedDigit()
                        }
                        if section == .run && state.isRunning {
                            ProgressView().controlSize(.mini)
                        }
                    }
                    .tag(section)
                    .padding(.vertical, 3)
                }
            }
            .listStyle(.sidebar)

            Divider()
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    Circle().fill(lastRunColor).frame(width: 7, height: 7)
                    Text(lastRunText).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
                }
                HStack(spacing: 8) {
                    Circle().fill(state.hasGeminiKey ? AppTheme.green : AppTheme.amber).frame(width: 7, height: 7)
                    Text(state.hasGeminiKey ? "Gemini hazır" : "Gemini anahtarı yok").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    Spacer(minLength: 4)
                    Text((Bundle.main.object(forInfoDictionaryKey: "AgenticStudioReleaseLabel") as? String) ?? "dev")
                        .font(.caption2.monospaced())
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .fixedSize()
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(13)
        }
    }
}
