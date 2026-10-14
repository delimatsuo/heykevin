import XCTest
@testable import Kevin

final class OnboardingServicesDraftTests: XCTestCase {

    func testAddTypedEntrySupportsOptionalPricesAndInvertsReversedBounds() {
        var draft = OnboardingServicesDraft()

        // Service without price
        XCTAssertTrue(draft.addTypedEntry(name: "  Emergency Leak Inspection  ", minPriceText: "", maxPriceText: ""))
        XCTAssertEqual(draft.items.count, 1)
        XCTAssertEqual(draft.items[0].name, "Emergency Leak Inspection")
        XCTAssertEqual(draft.items[0].priceMin, 0)
        XCTAssertEqual(draft.items[0].priceMax, 0)
        XCTAssertNil(draft.items[0].formattedPrice)

        // Service with single price
        XCTAssertTrue(draft.addTypedEntry(name: "Diagnostic Visit", minPriceText: "$89", maxPriceText: ""))
        XCTAssertEqual(draft.items[1].priceMin, 89)
        XCTAssertEqual(draft.items[1].priceMax, 89)
        XCTAssertEqual(draft.items[1].formattedPrice, "$89")

        // Service with inverted min/max normalizes automatically
        XCTAssertTrue(draft.addTypedEntry(name: "Water Heater Install", minPriceText: "1800", maxPriceText: "1200"))
        XCTAssertEqual(draft.items[2].priceMin, 1200)
        XCTAssertEqual(draft.items[2].priceMax, 1800)
        XCTAssertEqual(draft.items[2].formattedPrice, "$1200 - $1800")
    }

    func testParseUploadedTextParsesCSVMultipleFormatsAndSkipsHeaders() {
        let sample = """
        Service,Min Price,Max Price
        Drain Cleaning,$95,$185
        - Water Heater Flush: $140 - $220
        * Slab Leak Detection | $250
        Repipe Consultation
        """

        let entries = OnboardingServicesDraft.parseUploadedText(sample)
        XCTAssertEqual(entries.count, 4)
        XCTAssertEqual(entries[0].name, "Drain Cleaning")
        XCTAssertEqual(entries[0].priceMin, 95)
        XCTAssertEqual(entries[0].priceMax, 185)

        XCTAssertEqual(entries[1].name, "Water Heater Flush")
        XCTAssertEqual(entries[1].priceMin, 140)
        XCTAssertEqual(entries[1].priceMax, 220)

        XCTAssertEqual(entries[2].name, "Slab Leak Detection")
        XCTAssertEqual(entries[2].priceMin, 250)
        XCTAssertEqual(entries[2].priceMax, 250)

        XCTAssertEqual(entries[3].name, "Repipe Consultation")
        XCTAssertEqual(entries[3].priceMin, 0)
        XCTAssertEqual(entries[3].priceMax, 0)
    }

    func testImportUploadedTextDeduplicatesCaseInsensitivelyAndBuildsPayloadAndMarkdown() {
        var draft = OnboardingServicesDraft()
        _ = draft.addTypedEntry(name: "Drain Cleaning", minPriceText: "95", maxPriceText: "185")

        let importedCount = draft.importUploadedText(
            "drain cleaning, 100, 200\nSewer Camera Inspection: $199\nGeneral Plumbing Repair",
            fileName: "prices.csv"
        )
        XCTAssertEqual(importedCount, 2)
        XCTAssertEqual(draft.items.count, 3)
        XCTAssertEqual(draft.uploadedFileName, "prices.csv")

        draft.additionalNotes = "$89 service call waived with repair."

        let payload = draft.servicesPayload
        XCTAssertEqual(payload.count, 3)
        XCTAssertEqual(payload[0]["name"] as? String, "Drain Cleaning")
        XCTAssertEqual(payload[0]["price_min"] as? Int, 95)
        XCTAssertEqual(payload[0]["price_max"] as? Int, 185)
        XCTAssertEqual(payload[2]["name"] as? String, "General Plumbing Repair")
        XCTAssertEqual(payload[2]["price_min"] as? Int, 0)
        XCTAssertEqual(payload[2]["price_max"] as? Int, 0)

        let markdown = draft.knowledgeMarkdown
        XCTAssertTrue(markdown.contains("## Services"))
        XCTAssertTrue(markdown.contains("- Drain Cleaning ($95-$185)"))
        XCTAssertTrue(markdown.contains("- Sewer Camera Inspection ($199)"))
        XCTAssertTrue(markdown.contains("- General Plumbing Repair"))
        XCTAssertFalse(markdown.contains("General Plumbing Repair ($0"))
        XCTAssertTrue(markdown.contains("$89 service call waived with repair."))
    }

    func testEmptyDraftProducesEmptyKnowledgeAndEmptyPayload() {
        let draft = OnboardingServicesDraft()
        XCTAssertFalse(draft.hasContent)
        XCTAssertTrue(draft.servicesPayload.isEmpty)
        XCTAssertTrue(draft.knowledgeMarkdown.isEmpty)
    }
}
