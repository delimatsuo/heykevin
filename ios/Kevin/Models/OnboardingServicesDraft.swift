import Foundation
import PDFKit
import Vision

struct OnboardingServiceEntry: Identifiable, Equatable, Sendable {
    let id: UUID
    var name: String
    var priceMin: Int
    var priceMax: Int

    init(id: UUID = UUID(), name: String, priceMin: Int = 0, priceMax: Int = 0) {
        self.id = id
        self.name = name
        self.priceMin = priceMin
        self.priceMax = priceMax
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
    var additionalNotes: String = ""
    var uploadedFileName: String?

    var hasContent: Bool {
        !items.isEmpty || !additionalNotes.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
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
            guard items.count < Self.maxItems else { break }
            let exists = items.contains {
                $0.name.caseInsensitiveCompare(entry.name) == .orderedSame
            }
            if !exists {
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
        items.prefix(Self.maxItems).map { item in
            [
                "name": item.name,
                "price_min": item.priceMin,
                "price_max": item.priceMax,
            ]
        }
    }

    /// Freeform notes section for the contractor knowledge base.
    /// Structured services are stored in `services` (via `servicesPayload`) rather than
    /// duplicated into `knowledge`, so subsequent edits or deletions in `ServicesView`
    /// never leave stale prices in `knowledge`.
    var knowledgeMarkdown: String {
        let cleanNotes = additionalNotes.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanNotes.isEmpty else { return "" }
        let section = "## Pricing & Service Notes\n\(cleanNotes)"
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
