import Foundation
import XCTest
@testable import Kevin

final class AppVersionTests: XCTestCase {
    func testMarketingVersionWithValidShortVersionString() {
        let infoDictionary: [String: Any] = [
            "CFBundleShortVersionString": "1.2.10"
        ]
        XCTAssertEqual(AppVersionService.marketingVersion(from: infoDictionary), "1.2.10")
    }

    func testMarketingVersionWithNilDictionary() {
        XCTAssertEqual(AppVersionService.marketingVersion(from: nil), "0.0.0")
    }

    func testMarketingVersionWithAbsentKey() {
        let infoDictionary: [String: Any] = [
            "CFBundleName": "Kevin"
        ]
        XCTAssertEqual(AppVersionService.marketingVersion(from: infoDictionary), "0.0.0")
    }

    func testMarketingVersionWithNonStringValue() {
        let infoDictionary: [String: Any] = [
            "CFBundleShortVersionString": 123
        ]
        XCTAssertEqual(AppVersionService.marketingVersion(from: infoDictionary), "0.0.0")
    }
}
