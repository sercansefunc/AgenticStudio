import SwiftUI

struct LeadsView: View {
    @EnvironmentObject var state: AppState

    // HStack, not HSplitView: the AppKit split ignores the sidebar inset and slid the window content sideways.
    var body: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                filters
                Divider()
                let leads = state.visibleLeads
                if leads.isEmpty {
                    VStack(spacing: 8) {
                        Image(systemName: "tray").font(.system(size: 28)).foregroundStyle(.tertiary)
                        Text(state.leads.isEmpty ? "Henüz fırsat yok. Tara ile başla." : "Bu filtreye uyan fırsat yok.")
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    List(leads, selection: $state.selectedLeadID) { lead in
                        LeadRow(lead: lead).tag(lead.id)
                    }
                    .listStyle(.inset)
                }
            }
            .frame(minWidth: 320, idealWidth: 400, maxWidth: 440)

            Divider()

            Group {
                if let lead = state.selectedLead {
                    LeadDetailView(lead: lead).id(lead.id)
                } else {
                    Text("Bir fırsat seç").foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .frame(minWidth: 380, maxWidth: .infinity)
        }
    }

    private var filters: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Local field, not .searchable: a toolbar search that comes and goes per section re-lays out the window.
            TextField("Fırsatlarda ara", text: $state.searchText).textFieldStyle(.roundedBorder)
            HStack {
                Picker("Triage", selection: $state.verdictFilter) {
                    ForEach(VerdictFilter.allCases) { Text($0.rawValue).tag($0) }
                }
                Picker("Durum", selection: $state.statusFilter) {
                    ForEach(StatusFilter.allCases) { Text($0.rawValue).tag($0) }
                }
                Picker("Kaynak", selection: $state.sourceFilter) {
                    ForEach(state.sources, id: \.self) { Text($0).tag($0) }
                }
                Spacer(minLength: 0)
                Text("\(state.visibleLeads.count) / \(state.leads.count)").font(.callout).monospacedDigit().foregroundStyle(.secondary)
            }
            .labelsHidden()
            .controlSize(.small)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }
}

struct LeadRow: View {
    let lead: Lead

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                VerdictPill(verdict: lead.verdict)
                if lead.isStrong { StrongPill() }
                if lead.status != "new" { StatusPill(status: lead.status) }
                Spacer()
                Text(ISODate.relative(lead.postedDate)).font(.caption2).foregroundStyle(.tertiary)
            }
            Text(lead.title).font(.body.weight(.medium)).lineLimit(2)
            HStack(spacing: 10) {
                Text(lead.source)
                Text(lead.budget)
                if let competition = lead.competitionText { Text(competition) }
            }
            .font(.caption).foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
    }
}

/// Confirms in place: the label morphs to "Kopyalandı" where the click happened, no toast.
struct CopyLinkButton: View {
    @EnvironmentObject var state: AppState
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var copied = false
    let lead: Lead

    var body: some View {
        Button {
            state.copyLink(lead)
            copied = true
            Task {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                copied = false
            }
        } label: {
            ZStack {
                // Both labels stay in layout so the button never changes width.
                Label("Kopyalandı", systemImage: "checkmark").opacity(0)
                Label("Bağlantıyı kopyala", systemImage: "link").opacity(0)
                if copied {
                    Label("Kopyalandı", systemImage: "checkmark").transition(.blurFade(reduceMotion: reduceMotion))
                } else {
                    Label("Bağlantıyı kopyala", systemImage: "link").transition(.blurFade(reduceMotion: reduceMotion))
                }
            }
            .animation(Motion.exit, value: copied)
        }
        .keyboardShortcut("c", modifiers: [.command, .shift])
        .help("Bağlantıyı kopyala (⇧⌘C)")
    }
}

struct LeadDetailView: View {
    @EnvironmentObject var state: AppState
    let lead: Lead

    private static let order = ["proposal_sent", "won", "delivered", "lost", "rejected"]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(spacing: 6) {
                    VerdictPill(verdict: lead.verdict)
                    if lead.isStrong { StrongPill() }
                    StatusPill(status: lead.status)
                    Spacer()
                }
                Text(lead.title).font(.title2.weight(.semibold)).textSelection(.enabled)
                HStack(spacing: 14) {
                    Label(lead.source, systemImage: "globe")
                    Label(lead.budget, systemImage: "banknote")
                    if let competition = lead.competitionText { Label(competition, systemImage: "person.3") }
                    if let hours = lead.effortHours { Label("~\(hours) saat", systemImage: "hourglass") }
                    if let fit = lead.fitText { Label(fit, systemImage: "person.crop.circle.badge.checkmark") }
                    Label(ISODate.relative(lead.postedDate), systemImage: "clock")
                }
                .font(.callout).foregroundStyle(.secondary)

                if let summary = lead.triageSummary {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(summary).textSelection(.enabled)
                        if !lead.reasons.isEmpty {
                            Text(lead.reasons.joined(separator: " · ")).font(.caption.monospaced()).foregroundStyle(.secondary)
                        }
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .appPanel()
                }

                FeedbackPanel(lead: lead)

                HStack {
                    Button { state.open(lead) } label: { Label("İlanı aç", systemImage: "safari") }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut("o", modifiers: .command)
                        .help("İlanı tarayıcıda aç (⌘O)")
                    CopyLinkButton(lead: lead)
                }

                if !lead.transitions.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Durumu değiştir").font(.headline)
                        HStack {
                            ForEach(Self.order.filter(lead.transitions.contains), id: \.self) { target in
                                Button(AppTheme.statusTitle(target)) { Task { await state.mark(lead, as: target) } }
                                    .tint(target == "rejected" ? AppTheme.red : nil)
                            }
                            if state.busyLeadIDs.contains(lead.id) { ProgressView().controlSize(.small) }
                        }
                        .disabled(state.busyLeadIDs.contains(lead.id))
                        Text("Durum yalnız kayıttır; teklif/mesaj gönderimi her zaman senin elinde.")
                            .font(.caption2).foregroundStyle(.tertiary)
                    }
                }

                Divider()
                Text(lead.summary).font(.body).lineSpacing(2).textSelection(.enabled)
            }
            .padding(24)
            .frame(maxWidth: 720, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// Owner's verdict next to triage's; saved to feedback.json so triage rules can learn from it.
struct FeedbackPanel: View {
    @EnvironmentObject var state: AppState
    let lead: Lead
    @State private var note: String

    private static let choices = [("review", "İyi iş", "hand.thumbsup"), ("maybe", "Belki", "questionmark.circle"),
                                  ("reject", "Kötü", "hand.thumbsdown")]

    init(lead: Lead) {
        self.lead = lead
        _note = State(initialValue: lead.feedback?.note ?? "")
    }

    private var agreement: String? {
        guard let label = lead.feedback?.label, let verdict = lead.verdict else { return nil }
        return label == verdict ? "Triajla aynı" : "Triaj: \(AppTheme.verdictTitle(verdict)) — farklı"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Senin değerlendirmen").font(.headline)
                Spacer()
                if let agreement { Text(agreement).font(.caption).foregroundStyle(.secondary) }
            }
            HStack {
                ForEach(Self.choices, id: \.0) { label, title, icon in
                    let selected = lead.feedback?.label == label
                    // Clicking the selected choice again clears it.
                    Button { save(selected ? nil : label) } label: {
                        Label(title, systemImage: selected ? "checkmark.circle.fill" : icon)
                    }
                    .tint(selected ? AppTheme.verdictColor(label) : nil)
                }
            }
            HStack(alignment: .top) {
                TextField("Neden? (kısa not)", text: $note, axis: .vertical)
                    .lineLimit(1...4)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { save(lead.feedback?.label) }
                Button("Notu kaydet") { save(lead.feedback?.label) }
                    .disabled(lead.feedback == nil || note == lead.feedback?.note)
            }
        }
        .disabled(state.busyLeadIDs.contains(lead.id))
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .appPanel()
    }

    private func save(_ label: String?) {
        guard label != nil || lead.feedback != nil else { return }
        Task { await state.rate(lead, label: label, note: label == nil ? "" : note) }
        if label == nil { note = "" }
    }
}
