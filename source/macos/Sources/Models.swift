import Foundation

enum AppSection: String, CaseIterable, Identifiable {
    case overview = "Özet"
    case leads = "Fırsatlar"
    case reports = "Raporlar"
    case run = "Tarama"
    case settings = "Ayarlar"

    var id: String { rawValue }
    var icon: String {
        switch self {
        case .overview: return "chart.bar.xaxis"
        case .leads: return "tray.full"
        case .reports: return "doc.text"
        case .run: return "arrow.triangle.2.circlepath"
        case .settings: return "gearshape"
        }
    }
}

enum NoticeKind { case info, success, warning, error }

struct Notice: Identifiable {
    let id = UUID()
    let kind: NoticeKind
    let text: String
    let code: String?
    let recovery: String?
}

/// Every bridge reply carries `ok`; `error` is set when `ok` is false.
struct Envelope: Decodable {
    let ok: Bool
    let error: String?
}

struct RunInfo: Decodable, Equatable {
    let day: String
    let startedAt: String
    let finishedAt: String?
    let exitCode: Int?
}

struct StatusResponse: Decodable {
    let today: String
    let lastRun: RunInfo?
    let needsRun: Bool
    let reports: [String]
}

struct RunResponse: Decodable {
    let day: String
    let exitCode: Int
    let report: String
}

struct Lead: Decodable, Identifiable, Hashable {
    let id: String
    let status: String
    let source: String
    let url: String
    let title: String
    let summary: String
    let postedAt: String?
    let pricingModel: String
    let currency: String?
    let budgetMin: String?
    let budgetMax: String?
    let budgetMaxUsd: Int?
    let competition: Int?
    let bids: Int?
    let fit: String?
    let verdict: String?
    let reasons: [String]
    let triageSummary: String?
    let effortHours: Int?
    let feedback: LeadFeedback?
    let transitions: [String]

    var budget: String {
        func amount(_ text: String) -> String { text.hasSuffix(".0") ? String(text.dropLast(2)) : text }
        guard let min = budgetMin.map(amount) else { return "bütçe yok" }
        let max = budgetMax.map(amount)
        let range = (max == nil || max == min) ? min : "\(min)–\(max!)"
        let kind = pricingModel == "hourly" ? "/saat" : ""
        let usd = (currency != "USD" && budgetMaxUsd != nil) ? " (≈$\(budgetMaxUsd!))" : ""
        return "\(range) \(currency ?? "")\(kind)\(usd)".trimmingCharacters(in: .whitespaces)
    }

    /// Raw bid/reply count when the source has it; otherwise the coarse estimate.
    var competitionText: String? {
        if let bids { return "\(bids) teklif" }
        return competition.map { "rekabet \($0)" }
    }

    /// Gemini's judgement of profile fit; nil before triage.
    var fitText: String? {
        ["full": "profil tam", "partial": "profil kısmi", "none": "profil dışı"][fit ?? ""]
    }

    var postedDate: Date? { postedAt.flatMap(ISODate.parse) }

    /// Deterministic shortlist over the triage: review, real money, not swamped, fresh.
    var isStrong: Bool {
        guard verdict == "review", isActive else { return false }
        let fresh = postedDate.map { $0 > Date().addingTimeInterval(-4 * 86_400) } ?? true
        return (competition ?? 0) < 80 && (budgetMaxUsd ?? 10) >= 10 && fresh
    }

    var isActive: Bool { !["rejected", "lost", "delivered", "won"].contains(status) }

    var verdictRank: Int {
        if isStrong { return -1 }
        switch verdict {
        case "review": return 0
        case "maybe": return 1
        case nil: return 2
        default: return 4
        }
    }
}

/// Owner's own verdict on a lead; feeds triage improvements.
struct LeadFeedback: Decodable, Hashable {
    let label: String
    let note: String
}

struct LeadsResponse: Decodable { let leads: [Lead] }
struct MarkResponse: Decodable { let lead: Lead }

struct DeskSettings: Codable, Equatable {
    var market_a: Bool
    var board: Bool
    var market_b: Bool
    var reddit: Bool
    var forums: Bool
    var hnLatest: Bool
    var hnThreads: [Int]
    var bountyhub: Bool
    var opire: Bool
    var issuehunt: Bool
    var githubBountyMaxCandidates: Int
    var githubBountyMaxCommentPages: Int
    var minimumConfidence: Int
    var triage: Bool
    var geminiDailyCap: Int
    var quickScan: Bool
}

struct SettingsResponse: Decodable { let settings: DeskSettings }

enum ISODate {
    private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]; return f
    }()
    private static let fractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
    }()

    static func parse(_ text: String) -> Date? { plain.date(from: text) ?? fractional.date(from: text) }

    private static let relativeFormatter: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter(); f.locale = Locale(identifier: "tr_TR"); f.unitsStyle = .short; return f
    }()
    private static let clockFormatter: DateFormatter = {
        let f = DateFormatter(); f.locale = Locale(identifier: "tr_TR"); f.dateFormat = "d MMM HH:mm"; return f
    }()

    static func relative(_ date: Date?) -> String {
        guard let date else { return "tarih yok" }
        return relativeFormatter.localizedString(for: date, relativeTo: Date())
    }

    static func clock(_ text: String?) -> String {
        guard let text, let date = parse(text) else { return "—" }
        return clockFormatter.string(from: date)
    }
}

/// Marketplaces where each proposal spends a limited monthly allowance.
let limitedSources = ["market_a", "market_b"]

struct CountItem: Identifiable {
    let label: String
    let count: Int
    var id: String { label }
}

/// Dashboard numbers, derived once per lead reload.
struct Overview {
    var open = 0
    var suggested = 0
    var strong = 0
    var fresh = 0
    var tracked = 0
    var untriaged = 0
    var verdicts: [CountItem] = []
    var sources: [CountItem] = []
    var days: [(day: Date, count: Int)] = []
    var rejectReasons: [CountItem] = []
    var top: [Lead] = []
    var topLimited: [Lead] = []

    static let reasonTitles = [
        "not_software": "yazılım değil", "gray_or_harmful": "gri / zararlı", "too_large": "çok büyük",
        "too_cheap": "çok ucuz", "academic": "akademik", "employment_role": "işe alım / uzun dönem",
        "outside_profile": "profil dışı", "high_competition": "yüksek rekabet", "unclear_scope": "kapsam belirsiz",
        "eligibility": "uygunluk şartı", "not_a_job": "iş değil", "not_open": "kapanmış",
    ]

    init() {}

    init(leads: [Lead], now: Date = Date()) {
        let active = leads.filter(\.isActive)
        open = active.count
        suggested = active.filter { $0.verdict == "review" || $0.verdict == "maybe" }.count
        strong = active.filter(\.isStrong).count
        fresh = active.filter { ($0.postedDate ?? .distantPast) > now.addingTimeInterval(-86_400) }.count
        tracked = leads.filter { StatusFilter.tracked.matches($0) }.count
        untriaged = active.filter { $0.verdict == nil }.count
        verdicts = [("review", "İncele"), ("maybe", "Belki"), ("reject", "Ele"),
                    (nil, "Triage yok")].map { key, title in
            CountItem(label: title, count: active.filter { $0.verdict == key }.count)
        }
        sources = Dictionary(grouping: active, by: \.source)
            .map { CountItem(label: $0.key, count: $0.value.count) }
            .sorted { $0.count > $1.count }
        let calendar = Calendar.current
        let start = calendar.startOfDay(for: now)
        let byDay = Dictionary(grouping: active.compactMap(\.postedDate)) { calendar.startOfDay(for: $0) }
        days = (0..<14).reversed().compactMap { offset in
            calendar.date(byAdding: .day, value: -offset, to: start).map { ($0, byDay[$0]?.count ?? 0) }
        }
        var reasons: [String: Int] = [:]
        for lead in active where lead.verdict == "reject" { for reason in lead.reasons { reasons[reason, default: 0] += 1 } }
        rejectReasons = reasons.map { CountItem(label: Overview.reasonTitles[$0.key] ?? $0.key, count: $0.value) }
            .sorted { $0.count > $1.count }.prefix(6).map { $0 }
        let ranked = active.filter { $0.verdict == "review" || $0.verdict == "maybe" }
            .sorted { a, b in
                a.verdictRank != b.verdictRank ? a.verdictRank < b.verdictRank
                    : (a.postedDate ?? .distantPast) > (b.postedDate ?? .distantPast)
            }
        top = ranked.filter { !limitedSources.contains($0.source) }.prefix(8).map { $0 }
        // A limited proposal is spent only on the strongest leads.
        topLimited = ranked.filter { limitedSources.contains($0.source) && $0.isStrong }.prefix(5).map { $0 }
    }
}
