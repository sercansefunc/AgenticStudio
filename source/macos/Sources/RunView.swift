import SwiftUI

struct RunView: View {
    @EnvironmentObject var state: AppState

    private var summary: String {
        guard let run = state.status?.lastRun else { return "Henüz tarama yapılmadı." }
        let result: String
        switch run.exitCode {
        case 0: result = "tamamlandı"
        case 75: result = "kısmen tamamlandı (bazı kaynaklar yanıt vermedi)"
        case nil: result = "yarım kaldı"
        case let code?: result = "başarısız (kod \(code))"
        }
        return "Son tarama \(run.day): \(result) · başlangıç \(ISODate.clock(run.startedAt)), bitiş \(ISODate.clock(run.finishedAt))"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(summary).font(.headline)
                Text("Uygulama her gün ilk açılışta bir kez tarar; istekler bu Mac'ten gider.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            RunLogView(log: state.runLog)
        }
        .padding(24)
    }
}

struct RunLogView: View {
    @ObservedObject var log: RunLog

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(log.lines.enumerated()), id: \.offset) { index, line in
                        Text(line)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(line.contains("retry required") || line.contains("Error") || line.contains("unexpected")
                                             ? AppTheme.red : Color.primary)
                            .textSelection(.enabled)
                            .id(index)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading).padding(13)
            }
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).stroke(AppTheme.border))
            .onChange(of: log.lines.count) { _ in
                if let last = log.lines.indices.last { proxy.scrollTo(last, anchor: .bottom) }
            }
        }
    }
}
