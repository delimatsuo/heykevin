import Foundation
import SwiftUI

/// Outcome of a version-gate check.
enum VersionGateState: Equatable {
    /// Not yet checked — proceed normally.
    case unknown
    /// Up to date.
    case current
    /// New version available — show the optional, dismissible nudge.
    case optional(latest: String, storeURL: String)
    /// Below the minimum supported version — block the user behind a forced gate.
    case forced(latest: String, storeURL: String, message: String?)
}

/// Polls the backend's `/api/app/version` endpoint on cold launch and decides
/// whether the running build is current, needs a soft nudge, or must be hard-blocked.
@MainActor
final class AppVersionService: ObservableObject {
    static let shared = AppVersionService()

    @Published var state: VersionGateState = .unknown

    private let currentVersion: String

    private static let lastOptionalNagKey = "kevin.update.lastOptionalNag"

    private init() {
        self.currentVersion = Self.marketingVersion()
    }

    nonisolated static func marketingVersion(from infoDictionary: [String: Any]? = Bundle.main.infoDictionary) -> String {
        infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.0.0"
    }

    func check() async {
        guard let url = URL(string: "\(AppState.shared.backendURL)/api/app/version?platform=ios") else {
            return
        }
        var req = URLRequest(url: url)
        req.timeoutInterval = 8
        req.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, response) = try await URLSession.shared.data(for: req)
            guard
                let http = response as? HTTPURLResponse,
                http.statusCode == 200,
                let info = try? JSONDecoder().decode(VersionResponse.self, from: data)
            else { return }
            updateState(from: info)
        } catch {
            // Silently ignore — don't gate the user on a transient network failure.
            return
        }
    }

    /// Called when the user dismisses the optional nudge — clears state until next cold launch.
    func dismissOptional() {
        if case .optional = state {
            state = .current
            UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: Self.lastOptionalNagKey)
        }
    }

    private func updateState(from info: VersionResponse) {
        let belowMin = compare(currentVersion, info.minVersion) < 0
        let belowLatest = compare(currentVersion, info.latestVersion) < 0

        if belowMin || (info.forceUpdate && belowLatest) {
            state = .forced(latest: info.latestVersion, storeURL: info.storeURL, message: info.updateMessage)
            return
        }

        if belowLatest, shouldShowOptionalNag() {
            state = .optional(latest: info.latestVersion, storeURL: info.storeURL)
            return
        }

        state = .current
    }

    private func shouldShowOptionalNag() -> Bool {
        let last = UserDefaults.standard.double(forKey: Self.lastOptionalNagKey)
        guard last > 0 else { return true }
        // Show at most once every 24 hours so we don't nag every cold launch.
        return Date().timeIntervalSince1970 - last > 24 * 3600
    }
}

/// Lexicographic numeric compare on dotted versions ("1.2.2" < "1.10.0").
/// Returns -1 / 0 / 1.
private func compare(_ a: String, _ b: String) -> Int {
    let aParts = a.split(separator: ".").map { Int($0) ?? 0 }
    let bParts = b.split(separator: ".").map { Int($0) ?? 0 }
    let count = max(aParts.count, bParts.count)
    for i in 0..<count {
        let av = i < aParts.count ? aParts[i] : 0
        let bv = i < bParts.count ? bParts[i] : 0
        if av < bv { return -1 }
        if av > bv { return 1 }
    }
    return 0
}

private struct VersionResponse: Decodable {
    let platform: String
    let minVersion: String
    let latestVersion: String
    let forceUpdate: Bool
    let updateMessage: String?
    let storeURL: String

    enum CodingKeys: String, CodingKey {
        case platform
        case minVersion    = "min_version"
        case latestVersion = "latest_version"
        case forceUpdate   = "force_update"
        case updateMessage = "update_message"
        case storeURL      = "store_url"
    }
}
