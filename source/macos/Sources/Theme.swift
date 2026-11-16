import SwiftUI

/// Semantic system colors only: follows light/dark, the owner's accent color and Increase Contrast.
struct AppTheme {
    static let fill = Color.primary.opacity(0.045)
    static let border = Color(nsColor: .separatorColor)
    static let accent = Color.accentColor
    static let green = Color(nsColor: .systemGreen)
    static let amber = Color(nsColor: .systemOrange)
    static let red = Color(nsColor: .systemRed)
    static let muted = Color.secondary

    static func statusColor(_ status: String) -> Color {
        switch status {
        case "won", "delivered": return green
        default: return muted
        }
    }

    static func statusTitle(_ status: String) -> String {
        switch status {
        case "new": return "Yeni"
        case "shortlisted": return "Kısa liste"
        case "investigating": return "İnceleniyor"
        case "pursuing": return "Takipte"
        case "proposal_sent": return "Teklif gönderildi"
        case "won": return "Kazanıldı"
        case "lost": return "Kaybedildi"
        case "delivered": return "Teslim edildi"
        case "rejected": return "Elendi"
        default: return status.replacingOccurrences(of: "_", with: " ")
        }
    }

    static func verdictColor(_ verdict: String?) -> Color {
        switch verdict {
        case "review": return green
        case "maybe": return amber
        case "reject": return red
        default: return muted
        }
    }

    static func verdictTitle(_ verdict: String?) -> String {
        switch verdict {
        case "review": return "İncele"
        case "maybe": return "Belki"
        case "reject": return "Ele"
        default: return "Triage yok"
        }
    }
}

/// Grouping by a quiet fill, no border or shadow: hierarchy comes from spacing and type.
struct PanelModifier: ViewModifier {
    func body(content: Content) -> some View {
        content
            .background(AppTheme.fill)
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

/// Motion tokens. Crisp dashboard: no bounce, UI under 300 ms, exits faster than entrances.
enum Motion {
    /// Critically damped spring: no overshoot, interruptible, starts from the live value.
    static let standard = Animation.spring(response: 0.3, dampingFraction: 1)
    /// Exits and state swaps: strong ease-out, never ease-in.
    static let exit = Animation.timingCurve(0.23, 1, 0.32, 1, duration: 0.18)
}

/// Crossfade with a light blur so two states read as one morph instead of two overlapping views.
private struct BlurFade: ViewModifier {
    let active: Bool
    func body(content: Content) -> some View {
        content.blur(radius: active ? 2 : 0).opacity(active ? 0 : 1).scaleEffect(active ? 0.96 : 1)
    }
}

extension AnyTransition {
    static func blurFade(reduceMotion: Bool) -> AnyTransition {
        reduceMotion ? .opacity : .modifier(active: BlurFade(active: true), identity: BlurFade(active: false))
    }
}

/// Clickable row/card: instant hover highlight and press-down feedback, spring release.
struct PressableStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { PressableBody(configuration: configuration) }
}

private struct PressableBody: View {
    let configuration: ButtonStyleConfiguration
    @State private var hovering = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let pressed = configuration.isPressed
        configuration.label
            .background(Color.primary.opacity(pressed ? 0.08 : (hovering ? 0.05 : 0)))
            .scaleEffect(pressed && !reduceMotion ? 0.98 : 1)
            // Press lands on mouse-down with no delay; only the release animates.
            .animation(pressed ? nil : Motion.standard, value: pressed)
            .onHover { hovering = $0 }
    }
}

extension View {
    func appPanel() -> some View { modifier(PanelModifier()) }
}

struct Pill: View {
    let title: String
    let color: Color
    var body: some View {
        Text(title)
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(color)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(color.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
    }
}

/// The one accented mark: worth spending a bid on.
struct StrongPill: View {
    var body: some View { Pill(title: "Güçlü", color: AppTheme.accent) }
}

struct StatusPill: View {
    let status: String
    var body: some View {
        Pill(title: AppTheme.statusTitle(status), color: AppTheme.statusColor(status))
            .animation(Motion.exit, value: status)
    }
}

struct VerdictPill: View {
    let verdict: String?
    var body: some View { Pill(title: AppTheme.verdictTitle(verdict), color: AppTheme.verdictColor(verdict)) }
}
