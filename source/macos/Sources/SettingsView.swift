import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var state: AppState
    @State private var draft: DeskSettings?
    @State private var threadsText = ""
    @State private var secretDrafts: [Secret: String] = [:]

    private var parsedThreads: [Int]? {
        let parts = threadsText.split(whereSeparator: { $0 == " " || $0 == "," })
        let ids = parts.compactMap { Int($0) }
        return ids.count == parts.count && ids.allSatisfy({ $0 > 0 }) && Set(ids).count == ids.count ? ids : nil
    }

    private var hasSource: Bool {
        guard let d = draft else { return false }
        return d.market_a || d.board || d.market_b || d.reddit || d.forums || d.hnLatest || d.bountyhub || d.opire || d.issuehunt
            || !(parsedThreads ?? []).isEmpty
    }

    private var isDirty: Bool {
        guard var d = draft, let threads = parsedThreads else { return draft != nil }
        d.hnThreads = threads
        return d != state.settings
    }

    var body: some View {
        Group {
            if draft != nil {
                Form { sources; triage; keys; data }
                    .formStyle(.grouped)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .safeAreaInset(edge: .bottom) {
            HStack {
                if parsedThreads == nil { Text("HN thread ID'leri pozitif ve tekil sayı olmalı.").foregroundStyle(AppTheme.red) }
                else if !hasSource { Text("En az bir kaynak açık olmalı.").foregroundStyle(AppTheme.red) }
                Spacer()
                Button("Geri al") { reset() }.disabled(!isDirty)
                Button("Kaydet") { save() }
                    .keyboardShortcut("s", modifiers: .command)
                    .buttonStyle(.borderedProminent)
                    .disabled(!isDirty || parsedThreads == nil || !hasSource)
            }
            .font(.caption)
            .padding(12)
            .background(.bar)
        }
        .onAppear { reset() }
        .onChange(of: state.settings) { _ in if !isDirty { reset() } }
    }

    private func binding<T>(_ path: WritableKeyPath<DeskSettings, T>) -> Binding<T> {
        Binding(get: { draft![keyPath: path] }, set: { draft![keyPath: path] = $0 })
    }

    private var sources: some View {
        Section("Kaynaklar") {
            Toggle("MarketA", isOn: binding(\.market_a))
            Toggle("Board (alıcı ilanları)", isOn: binding(\.board))
            Toggle("MarketB", isOn: binding(\.market_b))
            Toggle("Reddit (r/slavelabour, r/forhire, r/DoneDirtCheap …)", isOn: binding(\.reddit))
            Toggle("Niş forumlar (n8n Jobs, Make Hire a Pro)", isOn: binding(\.forums))
            Toggle("Hacker News — son freelancer başlığı", isOn: binding(\.hnLatest))
            TextField("Ek HN thread ID'leri", text: $threadsText, prompt: Text("örn. 41234567 41234999"))
            Toggle("BountyHub (GitHub bounty)", isOn: binding(\.bountyhub))
            Toggle("Opire", isOn: binding(\.opire))
            Toggle("IssueHunt", isOn: binding(\.issuehunt))
            Toggle("Hızlı tarama: app açıkken 15 dk'da bir MarketA + MarketB + Board'un en yenileri, yeni 'incele' ilanına bildirim",
                   isOn: binding(\.quickScan))
            if draft!.bountyhub || draft!.opire {
                Stepper("GitHub bounty aday sınırı: \(draft!.githubBountyMaxCandidates)",
                        value: binding(\.githubBountyMaxCandidates), in: 1...5)
                Stepper("GitHub yorum sayfası: \(draft!.githubBountyMaxCommentPages)",
                        value: binding(\.githubBountyMaxCommentPages), in: 1...3)
            }
        }
    }

    private var triage: some View {
        Section("Değerlendirme") {
            Toggle("Gemini triage (her adaya incele/belki/ele)", isOn: binding(\.triage))
            Stepper("Günlük Gemini çağrı sınırı: \(draft!.geminiDailyCap)", value: binding(\.geminiDailyCap), in: 0...500, step: 10)
            Stepper("İnceleme güven eşiği: \(draft!.minimumConfidence)", value: binding(\.minimumConfidence), in: 60...100, step: 5)
        }
    }

    private var keys: some View {
        Section("Anahtarlar (Keychain)") {
            ForEach(Secret.allCases) { secretRow($0) }
        }
    }

    private func secretRow(_ secret: Secret) -> some View {
        let has = state.storedSecrets.contains(secret)
        let text = Binding(get: { secretDrafts[secret] ?? "" }, set: { secretDrafts[secret] = $0 })
        return HStack {
            Circle().fill(has ? AppTheme.green : AppTheme.amber).frame(width: 7, height: 7)
            SecureField(secret.title, text: text, prompt: Text(has ? "kayıtlı — değiştirmek için yaz" : "yapıştır"))
            Button("Kaydet") { state.saveSecret(text.wrappedValue, for: secret); secretDrafts[secret] = nil }
                .disabled(text.wrappedValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            if has { Button("Sil", role: .destructive) { state.deleteSecret(secret) } }
        }
    }

    private var data: some View {
        Section("Veri") {
            LabeledContent("Klasör", value: state.paths.home.path)
            Button("Klasörü Finder'da aç") { state.paths.openHome() }
        }
    }

    private func reset() {
        draft = state.settings
        threadsText = (state.settings?.hnThreads ?? []).map(String.init).joined(separator: " ")
    }

    private func save() {
        guard var d = draft, let threads = parsedThreads else { return }
        d.hnThreads = threads
        Task { if await state.saveSettings(d) { reset() } }
    }
}
