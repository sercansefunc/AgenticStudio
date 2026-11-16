import SwiftUI

struct ReportsView: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        HStack(spacing: 0) {
            List(state.status?.reports ?? [], id: \.self, selection: Binding(
                get: { state.selectedReport },
                set: { state.selectedReport = $0; state.loadReport() }
            )) { name in
                Text(Self.label(name)).tag(name)
            }
            .listStyle(.sidebar)
            .frame(width: 170)

            Divider()

            ScrollView {
                MarkdownText(source: state.reportText)
                    .padding(24)
                    .frame(maxWidth: 760, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    /// "2026-10-17_143205" -> "2026-10-17 14:32"; legacy day-only names stay as is.
    static func label(_ name: String) -> String {
        let parts = name.split(separator: "_")
        guard parts.count == 2, parts[1].count == 6 else { return name }
        let time = parts[1]
        return "\(parts[0]) \(time.prefix(2)):\(time.dropFirst(2).prefix(2))"
    }
}

/// Minimal block renderer for the daily report: headings, bullets and inline markdown.
struct MarkdownText: View {
    let source: String

    var body: some View {
        LazyVStack(alignment: .leading, spacing: 4) {
            ForEach(Array(source.components(separatedBy: "\n").enumerated()), id: \.offset) { _, line in
                row(line)
            }
        }
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func row(_ line: String) -> some View {
        if line.hasPrefix("# ") {
            inline(String(line.dropFirst(2))).font(.title.weight(.bold)).padding(.top, 10)
        } else if line.hasPrefix("## ") {
            inline(String(line.dropFirst(3))).font(.title3.weight(.semibold)).padding(.top, 8)
        } else if line.hasPrefix("### ") {
            inline(String(line.dropFirst(4))).font(.headline).padding(.top, 4)
        } else if let range = line.range(of: #"^\s*- "#, options: .regularExpression) {
            let depth = line.distance(from: line.startIndex, to: range.upperBound) / 2
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("•").foregroundStyle(.secondary)
                inline(String(line[range.upperBound...]))
            }
            .padding(.leading, CGFloat(max(0, depth - 1)) * 16)
        } else if line.trimmingCharacters(in: .whitespaces).isEmpty {
            Spacer().frame(height: 4)
        } else {
            inline(line)
        }
    }

    private func inline(_ text: String) -> Text {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return Text((try? AttributedString(markdown: text, options: options)) ?? AttributedString(text))
    }
}
