import Foundation
import PDFKit
import Vision

struct OnboardingPricePreset: Equatable, Sendable, Hashable {
    let label: String
    let priceMin: Int
    let priceMax: Int
}

struct OnboardingQuotingRule: Identifiable, Equatable, Sendable {
    let id: String
    let emoji: String
    let iconSystemName: String
    let title: String
    let subtitle: String
    let knowledgeLine: String

    static let catalog: [OnboardingQuotingRule] = [
        OnboardingQuotingRule(
            id: "ballpark",
            emoji: "",
            iconSystemName: "slider.horizontal.3",
            title: String(localized: "Share prices as ballpark ranges"),
            subtitle: String(localized: "Kevin quotes your min–max range and reminds callers final pricing is confirmed on-site."),
            knowledgeLine: "- Quote listed service prices as ballpark estimates only; final price is confirmed on-site."
        ),
        OnboardingQuotingRule(
            id: "waive_fee",
            emoji: "",
            iconSystemName: "arrow.uturn.backward.circle",
            title: String(localized: "$89 service call waived with repair"),
            subtitle: String(localized: "Let callers know there's a diagnostic visit fee that goes toward the job if they book."),
            knowledgeLine: "- Standard $89 service call / diagnostic fee is waived when the customer proceeds with the repair."
        ),
        OnboardingQuotingRule(
            id: "free_est",
            emoji: "",
            iconSystemName: "doc.text",
            title: String(localized: "Free estimates for big jobs"),
            subtitle: String(localized: "For replacements or remodels, Kevin offers a free no-obligation on-site estimate."),
            knowledgeLine: "- Major replacements, installations, and remodels include a free on-site estimate."
        ),
        OnboardingQuotingRule(
            id: "no_phone_price",
            emoji: "",
            iconSystemName: "lock",
            title: String(localized: "Never quote prices on the phone"),
            subtitle: String(localized: "Focus 100% on booking the lead—collect their name, address, and issue first."),
            knowledgeLine: "- Do not quote dollar figures over the phone; collect the caller's issue and address so we can quote on-site."
        ),
    ]
}

enum OnboardingTradeCategory: String, CaseIterable, Identifiable, Sendable {
    case plumbing
    case electrical
    case hvac
    case roofing
    case general
    case other

    var id: String { rawValue }

    var label: String {
        switch self {
        case .plumbing: return String(localized: "Plumbing & Drain")
        case .electrical: return String(localized: "Electrical")
        case .hvac: return String(localized: "HVAC & Climate")
        case .roofing: return String(localized: "Roofing & Exterior")
        case .general: return String(localized: "General Contracting")
        case .other: return String(localized: "Other Business")
        }
    }

    var emoji: String { "" }

    var iconSystemName: String {
        switch self {
        case .plumbing: return "wrench.and.screwdriver"
        case .electrical: return "bolt"
        case .hvac: return "wind"
        case .roofing: return "house"
        case .general: return "hammer"
        case .other: return "square.grid.2x2"
        }
    }

    var serviceTypeKey: String {
        switch self {
        case .plumbing: return "plumbing"
        case .electrical: return "electrical"
        case .hvac: return "hvac"
        case .roofing, .general, .other: return "general"
        }
    }

    var defaultServices: [OnboardingServiceEntry] {
        switch self {
        case .plumbing:
            return [
                OnboardingServiceEntry(
                    name: "Drain Cleaning",
                    priceMin: 95,
                    priceMax: 185,
                    emoji: "🌀",
                    isSelected: true,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$95–$185", priceMin: 95, priceMax: 185),
                        OnboardingPricePreset(label: "$150–$250", priceMin: 150, priceMax: 250),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "Water Heater Install / Repair",
                    priceMin: 1200,
                    priceMax: 1800,
                    emoji: "🔥",
                    isSelected: true,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$150–$350", priceMin: 150, priceMax: 350),
                        OnboardingPricePreset(label: "$1,200–$1,800", priceMin: 1200, priceMax: 1800),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "Leak Detection & Pipe Repair",
                    priceMin: 0,
                    priceMax: 0,
                    emoji: "💧",
                    isSelected: false,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$120–$250", priceMin: 120, priceMax: 250),
                        OnboardingPricePreset(label: "$200–$450", priceMin: 200, priceMax: 450),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "24/7 Emergency Service",
                    priceMin: 250,
                    priceMax: 250,
                    emoji: "🚨",
                    isSelected: false,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$199", priceMin: 199, priceMax: 199),
                        OnboardingPricePreset(label: "$250", priceMin: 250, priceMax: 250),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "Sewer Camera Inspection",
                    priceMin: 199,
                    priceMax: 299,
                    emoji: "📹",
                    isSelected: false,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$149", priceMin: 149, priceMax: 149),
                        OnboardingPricePreset(label: "$199–$299", priceMin: 199, priceMax: 299),
                    ]
                ),
            ]
        case .electrical:
            return [
                OnboardingServiceEntry(
                    name: "Panel Upgrade (200A)",
                    priceMin: 1800,
                    priceMax: 3200,
                    emoji: "⚡",
                    isSelected: true,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$1,800–$3,200", priceMin: 1800, priceMax: 3200),
                        OnboardingPricePreset(label: "$2,500–$4,000", priceMin: 2500, priceMax: 4000),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "EV Charger Installation",
                    priceMin: 450,
                    priceMax: 850,
                    emoji: "🔌",
                    isSelected: true,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$450–$850", priceMin: 450, priceMax: 850),
                        OnboardingPricePreset(label: "$600–$1,200", priceMin: 600, priceMax: 1200),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "Circuit & Outlet Troubleshooting",
                    priceMin: 0,
                    priceMax: 0,
                    emoji: "💡",
                    isSelected: false,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$125–$225", priceMin: 125, priceMax: 225),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "Recessed & Landscape Lighting",
                    priceMin: 0,
                    priceMax: 0,
                    emoji: "✨",
                    isSelected: false,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$150–$300", priceMin: 150, priceMax: 300),
                    ]
                ),
            ]
        case .hvac:
            return [
                OnboardingServiceEntry(
                    name: "Seasonal AC / Furnace Tune-Up",
                    priceMin: 89,
                    priceMax: 89,
                    emoji: "🌡️",
                    isSelected: true,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$89", priceMin: 89, priceMax: 89),
                        OnboardingPricePreset(label: "$129", priceMin: 129, priceMax: 129),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "No-Cool / No-Heat Diagnostic",
                    priceMin: 99,
                    priceMax: 189,
                    emoji: "❄️",
                    isSelected: true,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$99–$189", priceMin: 99, priceMax: 189),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "Full System Replacement",
                    priceMin: 0,
                    priceMax: 0,
                    emoji: "🏠",
                    isSelected: false,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$5,500–$12,000", priceMin: 5500, priceMax: 12000),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "Duct Cleaning & Airflow",
                    priceMin: 0,
                    priceMax: 0,
                    emoji: "💨",
                    isSelected: false,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$299–$499", priceMin: 299, priceMax: 499),
                    ]
                ),
            ]
        case .roofing:
            return [
                OnboardingServiceEntry(
                    name: "Storm & Leak Roof Inspection",
                    priceMin: 0,
                    priceMax: 0,
                    emoji: "🔍",
                    isSelected: true,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$150", priceMin: 150, priceMax: 150),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "Shingle & Flashing Repair",
                    priceMin: 350,
                    priceMax: 850,
                    emoji: "🛠️",
                    isSelected: true,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$350–$850", priceMin: 350, priceMax: 850),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "Gutter Cleaning & Guards",
                    priceMin: 0,
                    priceMax: 0,
                    emoji: "🌧️",
                    isSelected: false,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$175–$350", priceMin: 175, priceMax: 350),
                    ]
                ),
            ]
        case .general:
            return [
                OnboardingServiceEntry(
                    name: "Kitchen & Bath Remodeling",
                    priceMin: 0,
                    priceMax: 0,
                    emoji: "🛁",
                    isSelected: true,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$2,500–$15,000", priceMin: 2500, priceMax: 15000),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "Half-Day / Full-Day Repairs",
                    priceMin: 250,
                    priceMax: 600,
                    emoji: "🧰",
                    isSelected: true,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$250–$600", priceMin: 250, priceMax: 600),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "Drywall, Paint & Trim",
                    priceMin: 0,
                    priceMax: 0,
                    emoji: "🎨",
                    isSelected: false,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$180–$450", priceMin: 180, priceMax: 450),
                    ]
                ),
            ]
        case .other:
            return [
                OnboardingServiceEntry(
                    name: "Initial Consultation / Quote",
                    priceMin: 0,
                    priceMax: 0,
                    emoji: "📋",
                    isSelected: true,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$95", priceMin: 95, priceMax: 95),
                    ]
                ),
                OnboardingServiceEntry(
                    name: "Standard Service Visit",
                    priceMin: 120,
                    priceMax: 250,
                    emoji: "⚡",
                    isSelected: false,
                    presets: [
                        OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
                        OnboardingPricePreset(label: "$120–$250", priceMin: 120, priceMax: 250),
                    ]
                ),
            ]
        }
    }
}

struct OnboardingServiceEntry: Identifiable, Equatable, Sendable {
    let id: UUID
    var name: String
    var priceMin: Int
    var priceMax: Int
    var emoji: String
    var isSelected: Bool
    var presets: [OnboardingPricePreset]

    init(
        id: UUID = UUID(),
        name: String,
        priceMin: Int = 0,
        priceMax: Int = 0,
        emoji: String = "✨",
        isSelected: Bool = true,
        presets: [OnboardingPricePreset] = [
            OnboardingPricePreset(label: "No price", priceMin: 0, priceMax: 0),
            OnboardingPricePreset(label: "$95–$185", priceMin: 95, priceMax: 185),
            OnboardingPricePreset(label: "$200–$400", priceMin: 200, priceMax: 400),
        ]
    ) {
        self.id = id
        self.name = name
        self.priceMin = priceMin
        self.priceMax = priceMax
        self.emoji = emoji
        self.isSelected = isSelected
        self.presets = presets
    }

    var formattedPrice: String? {
        if priceMin <= 0 && priceMax <= 0 {
            return nil
        }
        if priceMin > 0 && (priceMax <= 0 || priceMin == priceMax) {
            return "$\(priceMin)"
        }
        if priceMin <= 0 && priceMax > 0 {
            return String(localized: "Up to $\(priceMax)")
        }
        return "$\(priceMin) - $\(priceMax)"
    }
}

struct OnboardingServicesDraft: Equatable, Sendable {
    static let maxItems = 50
    static let maxNameLength = 100
    static let maxPrice = 100_000
    static let maxKnowledgeLength = 10_000

    var items: [OnboardingServiceEntry] = []
    var selectedRuleIDs: Set<String> = []
    var additionalNotes: String = ""
    var uploadedFileName: String?

    var activeItems: [OnboardingServiceEntry] {
        items.filter(\.isSelected)
    }

    var hasContent: Bool {
        !activeItems.isEmpty
            || !selectedRuleIDs.isEmpty
            || !additionalNotes.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    mutating func applyTradeTemplate(_ trade: OnboardingTradeCategory) {
        items = trade.defaultServices
        if selectedRuleIDs.isEmpty {
            selectedRuleIDs = ["ballpark"]
        }
    }

    mutating func toggleService(id: UUID) {
        guard let idx = items.firstIndex(where: { $0.id == id }) else { return }
        items[idx].isSelected.toggle()
    }

    mutating func applyPricePreset(_ preset: OnboardingPricePreset, to id: UUID) {
        guard let idx = items.firstIndex(where: { $0.id == id }) else { return }
        items[idx].priceMin = preset.priceMin
        items[idx].priceMax = preset.priceMax
        items[idx].isSelected = true
    }

    mutating func toggleQuotingRule(id: String) {
        if id == "no_phone_price" {
            if selectedRuleIDs.contains(id) {
                selectedRuleIDs.remove(id)
            } else {
                selectedRuleIDs = ["no_phone_price"]
            }
        } else {
            selectedRuleIDs.remove("no_phone_price")
            if selectedRuleIDs.contains(id) {
                selectedRuleIDs.remove(id)
            } else {
                selectedRuleIDs.insert(id)
            }
        }
    }

    static func sanitizeName(_ raw: String) -> String {
        let allowed = CharacterSet.alphanumerics
            .union(.whitespaces)
            .union(CharacterSet(charactersIn: "-./&,()"))
        let filteredScalars = raw.unicodeScalars.filter { allowed.contains($0) }
        let cleaned = String(String.UnicodeScalarView(filteredScalars))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return String(cleaned.prefix(maxNameLength))
    }

    static func parseDollarInt(_ raw: String) -> Int? {
        let trimmed = raw
            .replacingOccurrences(of: "$", with: "")
            .replacingOccurrences(of: ",", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        if let intVal = Int(trimmed) {
            return max(0, min(maxPrice, intVal))
        }
        if let doubleVal = Double(trimmed), doubleVal.isFinite {
            return max(0, min(maxPrice, Int(doubleVal.rounded())))
        }
        return nil
    }

    static func normalizePrices(min rawMin: Int?, max rawMax: Int?) -> (Int, Int) {
        let pmin = max(0, min(maxPrice, rawMin ?? 0))
        let pmax = max(0, min(maxPrice, rawMax ?? 0))
        if pmin > 0 && pmax == 0 {
            return (pmin, pmin)
        }
        if pmax > 0 && pmin > pmax {
            return (pmax, pmin)
        }
        return (pmin, pmax)
    }

    @discardableResult
    mutating func addTypedEntry(name: String, minPriceText: String = "", maxPriceText: String = "") -> Bool {
        guard items.count < Self.maxItems else { return false }
        let cleanName = Self.sanitizeName(name)
        guard !cleanName.isEmpty else { return false }
        let parsedMin = Self.parseDollarInt(minPriceText)
        let parsedMax = Self.parseDollarInt(maxPriceText)
        let (pmin, pmax) = Self.normalizePrices(min: parsedMin, max: parsedMax)
        items.append(OnboardingServiceEntry(name: cleanName, priceMin: pmin, priceMax: pmax))
        return true
    }

    mutating func removeEntry(id: UUID) {
        items.removeAll { $0.id == id }
    }

    mutating func removeEntries(at offsets: IndexSet) {
        for index in offsets.sorted(by: >) where items.indices.contains(index) {
            items.remove(at: index)
        }
    }

    @discardableResult
    mutating func importUploadedText(_ rawText: String, fileName: String? = nil) -> Int {
        let parsed = Self.parseUploadedText(rawText)
        var addedCount = 0
        for entry in parsed {
            if let existingIdx = items.firstIndex(where: {
                $0.name.caseInsensitiveCompare(entry.name) == .orderedSame
            }) {
                if !items[existingIdx].isSelected {
                    items[existingIdx].isSelected = true
                    if entry.priceMin > 0 || entry.priceMax > 0 {
                        items[existingIdx].priceMin = entry.priceMin
                        items[existingIdx].priceMax = entry.priceMax
                    }
                    addedCount += 1
                }
            } else if items.count < Self.maxItems {
                items.append(entry)
                addedCount += 1
            }
        }
        if addedCount > 0, let fileName, !fileName.isEmpty {
            uploadedFileName = fileName
        }
        return addedCount
    }

    static func parseUploadedText(_ rawText: String) -> [OnboardingServiceEntry] {
        let trimmed = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return [] }

        let headerTokens: Set<String> = [
            "service", "services", "service name", "item", "description",
            "price", "pricing", "min", "max", "min price", "max price", "rate", "cost"
        ]

        var results: [OnboardingServiceEntry] = []
        let rangeRegex = try? NSRegularExpression(
            pattern: #"^(.+?)\s*(?:[\(\:\-–—\|]\s*|\s+)\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:[\-–—]|to)\s*\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*\)?$"#,
            options: [.caseInsensitive]
        )
        let singlePriceRegex = try? NSRegularExpression(
            pattern: #"^(.+?)\s*(?:[\(\:\-–—\|]\s*|\s+)\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:/\w+|\+)?\s*\)?$"#,
            options: [.caseInsensitive]
        )
        let inlineRangeCheckRegex = try? NSRegularExpression(
            pattern: #"\$\s*\d+\s*[\-–—]\s*\$?\s*\d+"#
        )
        let bulletStripRegex = try? NSRegularExpression(
            pattern: #"^(?:[\-\*•]+|\d+[\.\)])\s*"#
        )

        for rawLine in trimmed.components(separatedBy: .newlines) {
            guard results.count < maxItems else { break }
            var line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
            if line.isEmpty || line.hasPrefix("#") {
                continue
            }
            if let bulletStripRegex {
                let nsRange = NSRange(line.startIndex..<line.endIndex, in: line)
                line = bulletStripRegex.stringByReplacingMatches(in: line, range: nsRange, withTemplate: "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
            }
            if line.isEmpty {
                continue
            }

            let hasInlineDollarRange: Bool = {
                guard let inlineRangeCheckRegex else { return false }
                let nsRange = NSRange(line.startIndex..<line.endIndex, in: line)
                return inlineRangeCheckRegex.firstMatch(in: line, range: nsRange) != nil
            }()

            // 1. CSV / TSV row (quote-aware)
            if (line.contains(",") || line.contains("\t")) && !hasInlineDollarRange {
                let separator: Character = line.contains("\t") ? "\t" : ","
                let cols = splitCSVRow(line, delimiter: separator)
                    .map {
                        $0.trimmingCharacters(in: .whitespacesAndNewlines)
                            .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
                    }
                    .filter { !$0.isEmpty }
                if !cols.isEmpty {
                    if cols.allSatisfy({ headerTokens.contains($0.lowercased()) }) {
                        continue
                    }
                    let cleanName = sanitizeName(cols[0])
                    if !cleanName.isEmpty {
                        var numbers: [Int] = []
                        for col in cols.dropFirst().prefix(2) {
                            if let val = parseDollarInt(col) {
                                numbers.append(val)
                            }
                        }
                        if !numbers.isEmpty {
                            let (pmin, pmax) = normalizePrices(
                                min: numbers[0],
                                max: numbers.count > 1 ? numbers[1] : numbers[0]
                            )
                            results.append(OnboardingServiceEntry(name: cleanName, priceMin: pmin, priceMax: pmax))
                            continue
                        } else if cols.count == 1 {
                            results.append(OnboardingServiceEntry(name: cleanName, priceMin: 0, priceMax: 0))
                            continue
                        }
                    }
                }
            }

            // 2. Price range match: e.g. "Drain cleaning ($150 - $250)"
            let nsLine = line as NSString
            let fullRange = NSRange(location: 0, length: nsLine.length)
            if let match = rangeRegex?.firstMatch(in: line, range: fullRange), match.numberOfRanges >= 4 {
                let rawName = nsLine.substring(with: match.range(at: 1))
                    .trimmingCharacters(in: CharacterSet(charactersIn: " -–—:|()"))
                let minStr = nsLine.substring(with: match.range(at: 2))
                let maxStr = nsLine.substring(with: match.range(at: 3))
                let cleanName = sanitizeName(rawName)
                if !cleanName.isEmpty {
                    let (pmin, pmax) = normalizePrices(min: parseDollarInt(minStr), max: parseDollarInt(maxStr))
                    results.append(OnboardingServiceEntry(name: cleanName, priceMin: pmin, priceMax: pmax))
                    continue
                }
            }

            // 3. Single price match: e.g. "Service call fee: $89"
            if let match = singlePriceRegex?.firstMatch(in: line, range: fullRange), match.numberOfRanges >= 3 {
                let rawName = nsLine.substring(with: match.range(at: 1))
                    .trimmingCharacters(in: CharacterSet(charactersIn: " -–—:|()"))
                let priceStr = nsLine.substring(with: match.range(at: 2))
                let cleanName = sanitizeName(rawName)
                if !cleanName.isEmpty, let price = parseDollarInt(priceStr) {
                    let (pmin, pmax) = normalizePrices(min: price, max: price)
                    results.append(OnboardingServiceEntry(name: cleanName, priceMin: pmin, priceMax: pmax))
                    continue
                }
            }

            // 4. Plain service line without price
            if !headerTokens.contains(line.lowercased()) {
                let cleanName = sanitizeName(line)
                if !cleanName.isEmpty {
                    results.append(OnboardingServiceEntry(name: cleanName, priceMin: 0, priceMax: 0))
                }
            }
        }

        return results
    }

    static func extractText(from data: Data, pathExtension: String) async -> String {
        let ext = pathExtension.lowercased()
        if ext == "pdf" {
            if let doc = PDFDocument(data: data) {
                var pages: [String] = []
                for idx in 0..<doc.pageCount {
                    if let pageText = doc.page(at: idx)?.string, !pageText.isEmpty {
                        pages.append(pageText)
                    }
                }
                return pages.joined(separator: "\n")
            }
            return ""
        }

        if ["jpg", "jpeg", "png", "heic", "webp"].contains(ext) {
            return await recognizeTextInImageData(data)
        }

        if let utf8 = String(data: data, encoding: .utf8) {
            return utf8
        }
        if let latin1 = String(data: data, encoding: .isoLatin1) {
            return latin1
        }
        return ""
    }

    private static func recognizeTextInImageData(_ data: Data) async -> String {
        await withCheckedContinuation { continuation in
            let request = VNRecognizeTextRequest { req, _ in
                let observations = (req.results as? [VNRecognizedTextObservation]) ?? []
                let lines = observations.compactMap { $0.topCandidates(1).first?.string }
                continuation.resume(returning: lines.joined(separator: "\n"))
            }
            request.recognitionLevel = .accurate
            request.usesLanguageCorrection = true
            let handler = VNImageRequestHandler(data: data, options: [:])
            do {
                try handler.perform([request])
            } catch {
                continuation.resume(returning: "")
            }
        }
    }

    static func splitCSVRow(_ line: String, delimiter: Character) -> [String] {
        var fields: [String] = []
        var current = ""
        var inQuotes = false
        let chars = Array(line)
        var idx = 0
        while idx < chars.count {
            let ch = chars[idx]
            if ch == "\"" {
                if inQuotes, idx + 1 < chars.count, chars[idx + 1] == "\"" {
                    current.append("\"")
                    idx += 2
                    continue
                }
                inQuotes.toggle()
                idx += 1
                continue
            }
            if ch == delimiter && !inQuotes {
                fields.append(current)
                current = ""
                idx += 1
                continue
            }
            current.append(ch)
            idx += 1
        }
        fields.append(current)
        return fields
    }

    var servicesPayload: [[String: Any]] {
        activeItems.prefix(Self.maxItems).map { item in
            [
                "name": item.name,
                "price_min": item.priceMin,
                "price_max": item.priceMax,
            ]
        }
    }

    /// Freeform notes & selected quoting rules section for the contractor knowledge base.
    /// Structured services are stored in `services` (via `servicesPayload`) rather than
    /// duplicated into `knowledge`, so subsequent edits or deletions in `ServicesView`
    /// never leave stale prices in `knowledge`.
    var knowledgeMarkdown: String {
        let ruleLines = OnboardingQuotingRule.catalog
            .filter { selectedRuleIDs.contains($0.id) }
            .map(\.knowledgeLine)
        let cleanNotes = additionalNotes.trimmingCharacters(in: .whitespacesAndNewlines)
        var bodyParts: [String] = []
        if !ruleLines.isEmpty {
            bodyParts.append(ruleLines.joined(separator: "\n"))
        }
        if !cleanNotes.isEmpty {
            bodyParts.append(cleanNotes)
        }
        guard !bodyParts.isEmpty else { return "" }
        let section = "## Pricing & Service Notes\n" + bodyParts.joined(separator: "\n")
        return String(section.prefix(Self.maxKnowledgeLength))
    }

    static func mergeKnowledge(existing: String?, additionalSection: String?) -> String? {
        let cleanAddition = (additionalSection ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanAddition.isEmpty else { return nil }
        let cleanExisting = (existing ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if cleanExisting.isEmpty {
            return String(cleanAddition.prefix(maxKnowledgeLength))
        }
        if cleanExisting.contains(cleanAddition) {
            return cleanExisting
        }
        let combined = "\(cleanExisting)\n\n\(cleanAddition)"
        return String(combined.prefix(maxKnowledgeLength))
    }
}
