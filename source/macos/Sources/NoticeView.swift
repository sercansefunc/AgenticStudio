import SwiftUI

struct NoticeView: View {
    let notice: Notice
    let onDismiss: () -> Void

    var color: Color {
        switch notice.kind {
        case .info: return AppTheme.accent
        case .success: return AppTheme.green
        case .warning: return AppTheme.amber
        case .error: return AppTheme.red
        }
    }
    var icon: String {
        switch notice.kind {
        case .info: return "info.circle.fill"
        case .success: return "checkmark.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .error: return "xmark.octagon.fill"
        }
    }
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon)
                .foregroundStyle(color)
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 4) {
                Text(notice.text)
                    .font(.system(size: 13, weight: .medium))
                    .textSelection(.enabled)

                if let recovery = notice.recovery, !recovery.isEmpty {
                    Text(recovery)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }

                if let code = notice.code, !code.isEmpty {
                    Text("Hata kodu: \(code)")
                        .font(.caption2.monospaced())
                        .foregroundStyle(.tertiary)
                        .textSelection(.enabled)
                }
            }
            .help([notice.text, notice.recovery].compactMap { $0 }.joined(separator: "\n"))

            Spacer(minLength: 10)

            Button(action: onDismiss) {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: 22, height: 22)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Kapat")
        }
        .padding(.leading, 14)
        .padding(.trailing, 10)
        .padding(.vertical, 11)
        .background(.regularMaterial)
        .overlay(Rectangle().fill(color).frame(width: 3), alignment: .leading)
        .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).stroke(AppTheme.border))
        .shadow(color: .black.opacity(0.25), radius: 18, y: 6)
    }
}
