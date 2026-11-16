import Charts
import SwiftUI

struct OverviewView: View {
    var body: some View {
        ScrollView { OverviewContent() }
    }
}

struct OverviewContent: View {
    @EnvironmentObject var state: AppState

    private var o: Overview { state.overview }
    private let columns = [GridItem(.flexible(), spacing: 24, alignment: .top), GridItem(.flexible(), spacing: 24, alignment: .top)]

    // Order follows the decision: what to act on first, numbers next, analysis last.
    var body: some View {
        VStack(alignment: .leading, spacing: 28) {
            Text(runLine).font(.callout).foregroundStyle(.secondary)
            if o.open > 0 && o.untriaged == o.open { triageBanner }
            tiles
            topLeads
            LazyVGrid(columns: columns, spacing: 28) {
                card("Triage", subtitle: "açık fırsatlar") { verdictChart }
                card("Kaynaklar", subtitle: "açık fırsat sayısı") { barChart(o.sources) }
                card("Son 14 gün", subtitle: "ilan tarihine göre") { dayChart }
                card("Eleme nedenleri", subtitle: "Gemini'nin elediği ilanlar") {
                    if o.rejectReasons.isEmpty { empty("Henüz elenen ilan yok.") } else { barChart(o.rejectReasons) }
                }
            }
        }
        .padding(24)
        .frame(maxWidth: 1100, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var runLine: String {
        if state.isRunning { return "Tarama sürüyor…" }
        guard let run = state.status?.lastRun else { return "Henüz tarama yok." }
        switch run.exitCode {
        case 0: return "Son tarama \(ISODate.clock(run.finishedAt)) · tamamlandı"
        case 75: return "Son tarama \(ISODate.clock(run.finishedAt)) · bazı kaynaklar yanıt vermedi"
        case nil: return "Son tarama yarım kaldı · Tara ile yeniden başlat"
        case let code?: return "Son tarama başarısız (kod \(code))"
        }
    }

    private var triageBanner: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(AppTheme.amber)
            VStack(alignment: .leading, spacing: 2) {
                Text("Fırsatlar değerlendirilmedi").font(.system(size: 13, weight: .semibold))
                Text(state.hasGeminiKey
                     ? "Gemini triage bu taramada sonuç vermedi; Tara ile tekrar dene."
                     : "Gemini anahtarı yok. Ayarlar'dan ekleyip Tara'ya bas; ilanlar İncele/Belki/Ele olarak ayrılır.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if !state.hasGeminiKey { Button("Ayarlar") { state.section = .settings } }
        }
        .padding(12)
        .appPanel()
    }

    /// One metric row, no cards: only the number that drives action carries the accent.
    private var tiles: some View {
        HStack(alignment: .top, spacing: 0) {
            tile("Güçlü aday", o.strong, accent: true)
                .help("İncele · rekabet <80 · ≥$10 · ≤4 gün")
            tile("Önerilen", o.suggested).help("İncele + Belki")
            tile("Açık fırsat", o.open)
            tile("Son 24 saat", o.fresh)
            tile("Takipte", o.tracked)
        }
    }

    private func tile(_ title: String, _ value: Int, accent: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(value)").font(.system(size: 28, weight: .semibold, design: .rounded)).monospacedDigit()
                .foregroundStyle(accent ? AppTheme.accent : .primary)
                .contentTransition(.numericText())
                .animation(Motion.standard, value: value)
            Text(title).font(.callout).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: charts

    private func card<Content: View>(_ title: String, subtitle: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                Text(subtitle).font(.callout).foregroundStyle(.secondary)
            }
            content().frame(height: 170)
        }
    }

    private func empty(_ text: String) -> some View {
        Text(text).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// Verdict is a state: fixed status colors, always paired with its text label.
    private var verdictChart: some View {
        Chart(o.verdicts) { item in
            BarMark(x: .value("Adet", item.count), y: .value("Karar", item.label), height: .fixed(16))
                .foregroundStyle(color(forVerdict: item.label))
                .cornerRadius(4)
                .annotation(position: .trailing) { valueLabel(item.count) }
        }
        .chartXAxis(.hidden)
        .chartYAxis { AxisMarks(position: .leading) { _ in AxisValueLabel().foregroundStyle(AppTheme.muted) } }
    }

    private func color(forVerdict label: String) -> Color {
        switch label {
        case "İncele": return AppTheme.green
        case "Belki": return AppTheme.amber
        case "Ele": return AppTheme.red
        default: return AppTheme.muted
        }
    }

    private func barChart(_ items: [CountItem]) -> some View {
        Chart(items) { item in
            BarMark(x: .value("Adet", item.count), y: .value("Ad", item.label), height: .fixed(14))
                .foregroundStyle(neutralBar)
                .cornerRadius(4)
                .annotation(position: .trailing) { valueLabel(item.count) }
        }
        .chartXAxis(.hidden)
        .chartYAxis { AxisMarks(position: .leading) { _ in AxisValueLabel().foregroundStyle(AppTheme.muted) } }
    }

    private var dayChart: some View {
        Chart(o.days, id: \.day) { point in
            BarMark(x: .value("Gün", point.day, unit: .day), y: .value("İlan", point.count), width: .ratio(0.6))
                .foregroundStyle(neutralBar)
                .cornerRadius(4)
        }
        .chartXAxis {
            AxisMarks(values: .stride(by: .day, count: 3)) { _ in
                AxisValueLabel(format: .dateTime.day().month(.abbreviated)).foregroundStyle(AppTheme.muted)
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading) { _ in
                AxisGridLine().foregroundStyle(AppTheme.border)
                AxisValueLabel().foregroundStyle(AppTheme.muted)
            }
        }
    }

    private let neutralBar = Color.primary.opacity(0.28)

    private func valueLabel(_ value: Int) -> some View {
        Text("\(value)").font(.caption2).monospacedDigit().foregroundStyle(AppTheme.muted)
    }

    // MARK: top leads

    private var topLeads: some View {
        VStack(alignment: .leading, spacing: 24) {
            leadList("Öne çıkanlar · limitsiz kaynaklar",
                     note: "Board, Reddit, forumlar: teklif hakkı harcamaz, önce bunlar.",
                     leads: o.top,
                     empty: o.open == 0 ? "Henüz fırsat yok. Tara ile başla." : "Limitsiz kaynaklarda İncele/Belki ilan yok.")
            leadList("Teklif hakkına değenler · MarketA, MarketB",
                     note: "Yalnız Güçlü adaylar; teklif verdi işaretleyince hak bir azalır.",
                     leads: o.topLimited, empty: "Bugün teklif hakkı harcamaya değer ilan yok.")
            bidCounters
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var bidCounters: some View {
        HStack(spacing: 18) {
            ForEach(limitedSources, id: \.self) { source in
                let left = state.bidsLeft[source]
                HStack(spacing: 6) {
                    Text("\(source) kalan hak:").font(.callout).foregroundStyle(.secondary)
                    Text(left.map(String.init) ?? "—").font(.callout.monospacedDigit())
                        .foregroundStyle(left == 0 ? AppTheme.red : .primary)
                    Stepper("", value: Binding(get: { state.bidsLeft[source] ?? 0 },
                                               set: { state.bidsLeft[source] = max(0, $0) }), in: 0...999)
                        .labelsHidden()
                }
            }
        }
    }

    private func leadList(_ title: String, note: String, leads: [Lead], empty: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                Text(note).font(.callout).foregroundStyle(.secondary)
            }
            if leads.isEmpty {
                Text(empty).font(.callout).foregroundStyle(.tertiary).padding(.vertical, 6)
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(leads.enumerated()), id: \.element.id) { index, lead in
                        if index > 0 { Divider().padding(.leading, 12) }
                        leadRow(lead)
                    }
                }
                .appPanel()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func leadRow(_ lead: Lead) -> some View {
        Button { state.show(lead) } label: {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                VerdictPill(verdict: lead.verdict)
                if lead.isStrong { StrongPill() }
                VStack(alignment: .leading, spacing: 3) {
                    Text(lead.title).font(.body.weight(.medium)).lineLimit(1)
                    Text(lead.triageSummary ?? "").font(.callout).foregroundStyle(.secondary).lineLimit(2)
                }
                Spacer(minLength: 8)
                VStack(alignment: .trailing, spacing: 3) {
                    Text(lead.budget).font(.callout).monospacedDigit()
                    Text("\(lead.source) · \(ISODate.relative(lead.postedDate))").font(.caption2).foregroundStyle(.tertiary)
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 9)
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
    }
}
