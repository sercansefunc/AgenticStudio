import AppKit
import Foundation
import SwiftUI
import UserNotifications

enum VerdictFilter: String, CaseIterable, Identifiable {
    case suggested = "Önerilen"
    case review = "İncele"
    case maybe = "Belki"
    case reject = "Ele"
    case untriaged = "Triage yok"
    case all = "Tümü"
    var id: String { rawValue }

    func matches(_ lead: Lead) -> Bool {
        switch self {
        // Untriaged leads stay visible (sorted last) so a missing key never empties the list.
        case .suggested: return lead.verdict != "reject"
        case .review: return lead.verdict == "review"
        case .maybe: return lead.verdict == "maybe"
        case .reject: return lead.verdict == "reject"
        case .untriaged: return lead.verdict == nil
        case .all: return true
        }
    }
}

enum StatusFilter: String, CaseIterable, Identifiable {
    case active = "Açık"
    case tracked = "Takipte"
    case closed = "Kapanan"
    case all = "Tümü"
    var id: String { rawValue }

    func matches(_ lead: Lead) -> Bool {
        switch self {
        case .active: return lead.isActive
        case .tracked: return ["shortlisted", "investigating", "pursuing", "proposal_sent"].contains(lead.status)
        case .closed: return !lead.isActive
        case .all: return true
        }
    }
}

/// Scan output lives apart from AppState so streaming lines only redraw the log view.
@MainActor
final class RunLog: ObservableObject {
    @Published var lines: [String] = []
}

/// Shows notifications as banners even while the app is frontmost.
final class ForegroundNotifier: NSObject, UNUserNotificationCenterDelegate, Sendable {
    static let shared = ForegroundNotifier()
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}

@MainActor
final class AppState: ObservableObject {
    @Published var section: AppSection = .overview
    @Published var leads: [Lead] = [] { didSet { recomputeLeads(); recomputeOverview() } }
    @Published private(set) var visibleLeads: [Lead] = []
    @Published private(set) var suggestedCount = 0
    @Published private(set) var overview = Overview()
    @Published var status: StatusResponse?
    @Published var settings: DeskSettings?
    @Published var isRunning = false
    let runLog = RunLog()
    @Published var notice: Notice?
    /// Auto-dismiss waits while the pointer is on the notice, so it never vanishes mid-read.
    var noticeHovered = false
    @Published var verdictFilter: VerdictFilter = .suggested { didSet { recomputeLeads() } }
    @Published var statusFilter: StatusFilter = .active { didSet { recomputeLeads() } }
    @Published var sourceFilter = "Tümü" { didSet { recomputeLeads() } }
    @Published var searchText = "" { didSet { recomputeLeads() } }
    @Published var selectedLeadID: String?
    @Published var busyLeadIDs: Set<String> = []
    @Published var selectedReport: String?
    @Published var reportText = ""
    /// Proposals left this month on limited marketplaces; owner-entered, local only.
    @Published var bidsLeft: [String: Int] = (UserDefaults.standard.dictionary(forKey: "bidsLeft") as? [String: Int]) ?? [:] {
        didSet { UserDefaults.standard.set(bidsLeft, forKey: "bidsLeft") }
    }
    @Published var storedSecrets: Set<Secret> = []
    var hasGeminiKey: Bool { storedSecrets.contains(.gemini) }

    let paths = AppPaths.current()
    private lazy var backend = BackendService(home: paths.home)
    private var bootstrapped = false
    /// Review leads already seen; nil until the first load, so startup never notifies.
    private var knownReviewIDs: Set<String>?
    static let quickScanInterval: Duration = .seconds(15 * 60)

    var sources: [String] { ["Tümü"] + Array(Set(leads.map(\.source))).sorted() }

    /// Filtering/sorting runs only when leads or filters change, never per redraw.
    private func recomputeLeads() {
        let query = searchText.trimmingCharacters(in: .whitespaces)
        visibleLeads = leads.filter { lead in
            verdictFilter.matches(lead) && statusFilter.matches(lead)
                && (sourceFilter == "Tümü" || lead.source == sourceFilter)
                && (query.isEmpty || lead.title.localizedCaseInsensitiveContains(query)
                    || lead.summary.localizedCaseInsensitiveContains(query))
        }
        .map { ($0, $0.postedDate ?? .distantPast) }
        .sorted { a, b in
            if a.0.verdictRank != b.0.verdictRank { return a.0.verdictRank < b.0.verdictRank }
            return a.1 > b.1
        }
        .map(\.0)
        suggestedCount = leads.filter { $0.isActive && VerdictFilter.suggested.matches($0) }.count
    }

    private func recomputeOverview() {
        overview = Overview(leads: leads)
    }

    var selectedLead: Lead? { leads.first { $0.id == selectedLeadID } }

    // MARK: lifecycle

    func bootstrap() async {
        guard !bootstrapped else { return }
        bootstrapped = true
        do { try paths.prepare() } catch {
            showNotice("Veri klasörü oluşturulamadı.", .error, recovery: error.localizedDescription, autoDismiss: false)
            return
        }
        refreshKeys()
        await loadSettings()
        await refresh()
        // First open of the owner day scans once; while open, quick scans catch low-bid leads.
        if status?.needsRun == true { await scan() }
        if settings?.quickScan == true {
            UNUserNotificationCenter.current().delegate = ForegroundNotifier.shared
            _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
        }
        Task { await quickScanLoop() }
    }

    private func quickScanLoop() async {
        while true {
            try? await Task.sleep(for: Self.quickScanInterval)
            guard let settings, settings.quickScan, settings.market_a || settings.market_b || settings.board,
                  !isRunning else { continue }
            // A new owner day (app left open overnight) gets its full scan first.
            if status?.needsRun == true { await scan() } else { await scan(quick: true) }
        }
    }

    private func notifyNewReviews() {
        let reviews = leads.filter { $0.verdict == "review" && $0.status == "new" }
        defer { knownReviewIDs = (knownReviewIDs ?? []).union(reviews.map(\.id)) }
        guard let known = knownReviewIDs else { return }
        for lead in reviews where !known.contains(lead.id) {
            let content = UNMutableNotificationContent()
            content.title = "Yeni ilan: \(lead.source)"
            content.body = [lead.title, lead.budget, lead.competitionText].compactMap { $0 }.joined(separator: " · ")
            content.sound = .default
            UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: lead.id, content: content, trigger: nil))
        }
    }

    func refresh() async {
        do {
            status = try await backend.call(StatusResponse.self, ["status"])
            leads = try await backend.call(LeadsResponse.self, ["leads"]).leads
            notifyNewReviews()
            if selectedReport == nil || !(status?.reports.contains(selectedReport!) ?? false) {
                selectedReport = status?.reports.first
            }
            loadReport()
        } catch {
            showNotice("Veriler okunamadı.", .error, recovery: error.localizedDescription, autoDismiss: false)
        }
    }

    // MARK: scan

    /// quick: only fast-closing sources (bidding marketplaces, newest Board), silent unless it fails; never the day's full scan.
    func scan(quick: Bool = false) async {
        guard !isRunning else { return }
        if !quick && settings?.triage == true && !hasGeminiKey {
            showNotice("Gemini anahtarı yok; triage olmadan taranacak.", .warning,
                       recovery: "Ayarlar'dan anahtar ekleyebilirsin.")
        }
        isRunning = true
        runLog.lines = ["\(quick ? "Hızlı tarama" : "Tarama") başladı \(Date().formatted(date: .omitted, time: .shortened))"]
        defer { isRunning = false }
        do {
            let result = try await backend.run(quick ? ["run", "--quick"] : ["run"], environment: secrets()) { [weak self] line in
                self?.runLog.lines.append(line)
            }
            let reply = try backend.decode(RunResponse.self, from: result)
            if quick {
                if reply.exitCode != 0 { showNotice("Hızlı tarama kısmen tamamlandı.", .warning) }
                await refresh()
                return
            }
            if reply.exitCode == 0 {
                showNotice("Tarama tamamlandı.", .success)
            } else {
                showNotice("Tarama kısmen tamamlandı; bazı kaynaklar yanıt vermedi.", .warning,
                           recovery: "Ayrıntı raporun 'Collection status' bölümünde. Tekrar taramak güvenli.",
                           autoDismiss: false)
            }
            selectedReport = reply.report
        } catch {
            showNotice(quick ? "Hızlı tarama başarısız." : "Tarama başarısız.", .error,
                       recovery: error.localizedDescription, autoDismiss: quick)
        }
        await refresh()
    }

    func stopScan() { backend.stop() }

    private func secrets() -> [String: String] {
        var env: [String: String] = [:]
        for secret in Secret.allCases {
            if let value = KeychainService.load(secret) { env[secret.environment] = value }
        }
        return env
    }

    // MARK: leads

    func mark(_ lead: Lead, as target: String) async {
        busyLeadIDs.insert(lead.id)
        defer { busyLeadIDs.remove(lead.id) }
        do {
            let updated = try await backend.call(MarkResponse.self, ["mark", lead.id, target]).lead
            if let index = leads.firstIndex(where: { $0.id == updated.id }) { leads[index] = updated }
            if target == "proposal_sent", let left = bidsLeft[lead.source] {
                bidsLeft[lead.source] = max(0, left - 1)
            }
            showNotice("\(AppTheme.statusTitle(lead.status)) → \(AppTheme.statusTitle(target))", .success)
        } catch {
            showNotice("Durum değiştirilemedi.", .error, recovery: error.localizedDescription)
        }
    }

    /// nil label clears the owner's verdict.
    func rate(_ lead: Lead, label: String?, note: String) async {
        busyLeadIDs.insert(lead.id)
        defer { busyLeadIDs.remove(lead.id) }
        var arguments = ["feedback", lead.id, "--note", note]
        if let label { arguments += ["--label", label] }
        do {
            let updated = try await backend.call(MarkResponse.self, arguments).lead
            if let index = leads.firstIndex(where: { $0.id == updated.id }) { leads[index] = updated }
            showNotice(label == nil ? "Değerlendirme silindi" : "Değerlendirme kaydedildi", .success)
        } catch {
            showNotice("Değerlendirme kaydedilemedi.", .error, recovery: error.localizedDescription)
        }
    }

    func show(_ lead: Lead) {
        verdictFilter = .suggested
        statusFilter = .active
        sourceFilter = "Tümü"
        searchText = ""
        selectedLeadID = lead.id
        section = .leads
    }

    func open(_ lead: Lead) {
        guard let url = URL(string: lead.url), ["http", "https"].contains(url.scheme ?? "") else { return }
        NSWorkspace.shared.open(url)
    }

    func copyLink(_ lead: Lead) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(lead.url, forType: .string)
    }

    // MARK: reports

    func loadReport() {
        guard let day = selectedReport else { reportText = ""; return }
        reportText = (try? String(contentsOf: paths.report(day), encoding: .utf8)) ?? "Rapor okunamadı."
    }

    // MARK: settings

    func loadSettings() async {
        do {
            settings = try await backend.call(SettingsResponse.self, ["settings"]).settings
        } catch {
            showNotice("Ayarlar okunamadı.", .error, recovery: error.localizedDescription, autoDismiss: false)
        }
    }

    func saveSettings(_ draft: DeskSettings) async -> Bool {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        guard let data = try? encoder.encode(draft), let json = String(data: data, encoding: .utf8) else { return false }
        do {
            settings = try await backend.call(SettingsResponse.self, ["settings", "--set", json]).settings
            showNotice("Ayarlar kaydedildi.", .success)
            return true
        } catch {
            showNotice("Ayarlar kaydedilemedi.", .error, recovery: error.localizedDescription)
            return false
        }
    }

    func refreshKeys() {
        storedSecrets = Set(Secret.allCases.filter { KeychainService.load($0) != nil })
    }

    func saveSecret(_ value: String, for secret: Secret) {
        do {
            try KeychainService.save(value, for: secret)
            refreshKeys()
            showNotice("Anahtar Keychain'e kaydedildi.", .success)
        } catch {
            showNotice("Anahtar kaydedilemedi.", .error, recovery: error.localizedDescription)
        }
    }

    func deleteSecret(_ secret: Secret) {
        do {
            try KeychainService.delete(secret)
            refreshKeys()
            showNotice("Anahtar silindi.", .info)
        } catch {
            showNotice("Anahtar silinemedi.", .error, recovery: error.localizedDescription)
        }
    }

    // MARK: notices

    func dismissNotice() { withAnimation(Motion.standard) { notice = nil } }

    func showNotice(_ text: String, _ kind: NoticeKind, recovery: String? = nil, autoDismiss: Bool = true) {
        let n = Notice(kind: kind, text: text, code: nil, recovery: recovery)
        withAnimation(Motion.standard) { notice = n }
        if autoDismiss {
            Task {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                while noticeHovered && notice?.id == n.id { try? await Task.sleep(nanoseconds: 500_000_000) }
                if notice?.id == n.id { dismissNotice() }
            }
        }
    }
}
