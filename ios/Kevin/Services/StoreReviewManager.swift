import Foundation
import StoreKit
import SwiftUI
import UIKit

/// Manages App Store review prompts for Hey Kevin adhering strictly to App Store Review Guidelines.
///
/// Milestones:
/// - Screened calls completed >= 3
/// - Or appointment confirmed
///
/// Guard rails:
/// - Never prompt during an active call
/// - Do not prompt more than once for the same marketing version
/// - Minimum 60 days between review prompts
@MainActor
final class StoreReviewManager: ObservableObject {
    static let shared = StoreReviewManager()

    enum Keys {
        static let screenedCallCount = "kevin.storeReview.screenedCallCount"
        static let appointmentConfirmed = "kevin.storeReview.appointmentConfirmed"
        static let lastPromptedDate = "kevin.storeReview.lastPromptedDate"
        static let lastPromptedVersion = "kevin.storeReview.lastPromptedVersion"
    }

    static let callThreshold = 3
    static let minimumDaysBetweenPrompts = 60
    static let secondsPerDay: TimeInterval = 86_400

    private let userDefaults: UserDefaults
    private let currentDateProvider: () -> Date
    private let currentVersionProvider: () -> String
    private let isCallActiveProvider: () -> Bool
    private let reviewRequester: @MainActor () -> Void

    init(
        userDefaults: UserDefaults = .standard,
        currentDateProvider: @escaping () -> Date = { Date() },
        currentVersionProvider: @escaping () -> String = { AppVersionService.marketingVersion() },
        isCallActiveProvider: @escaping () -> Bool = {
            AppState.shared.hasActiveCall || AppState.shared.isOnCall || CallManager.shared.isOnCall
        },
        reviewRequester: @escaping @MainActor () -> Void = {
            StoreReviewManager.requestAppStoreReview()
        }
    ) {
        self.userDefaults = userDefaults
        self.currentDateProvider = currentDateProvider
        self.currentVersionProvider = currentVersionProvider
        self.isCallActiveProvider = isCallActiveProvider
        self.reviewRequester = reviewRequester
    }

    // MARK: - State

    var screenedCallCount: Int {
        get { userDefaults.integer(forKey: Keys.screenedCallCount) }
        set { userDefaults.set(newValue, forKey: Keys.screenedCallCount) }
    }

    var appointmentConfirmed: Bool {
        get { userDefaults.bool(forKey: Keys.appointmentConfirmed) }
        set { userDefaults.set(newValue, forKey: Keys.appointmentConfirmed) }
    }

    var lastPromptedDate: Date? {
        get {
            let timestamp = userDefaults.double(forKey: Keys.lastPromptedDate)
            return timestamp > 0 ? Date(timeIntervalSince1970: timestamp) : nil
        }
        set {
            if let date = newValue {
                userDefaults.set(date.timeIntervalSince1970, forKey: Keys.lastPromptedDate)
            } else {
                userDefaults.removeObject(forKey: Keys.lastPromptedDate)
            }
        }
    }

    var lastPromptedVersion: String? {
        get { userDefaults.string(forKey: Keys.lastPromptedVersion) }
        set {
            if let v = newValue {
                userDefaults.set(v, forKey: Keys.lastPromptedVersion)
            } else {
                userDefaults.removeObject(forKey: Keys.lastPromptedVersion)
            }
        }
    }

    // MARK: - Eligibility Check

    var isMilestoneMet: Bool {
        screenedCallCount >= Self.callThreshold || appointmentConfirmed
    }

    var isVersionEligible: Bool {
        guard let lastVersion = lastPromptedVersion, !lastVersion.isEmpty else { return true }
        return lastVersion != currentVersionProvider()
    }

    var isDateEligible: Bool {
        guard let lastDate = lastPromptedDate else { return true }
        let elapsed = currentDateProvider().timeIntervalSince(lastDate)
        let minimumInterval = TimeInterval(Self.minimumDaysBetweenPrompts) * Self.secondsPerDay
        return elapsed >= minimumInterval
    }

    var isEligibleForReview: Bool {
        #if DEBUG
        if AppStoreScreenshotFixtures.isEnabled { return false }
        #endif

        // Never prompt during an active call
        guard !isCallActiveProvider() else { return false }

        // Must meet at least one milestone
        guard isMilestoneMet else { return false }

        // Must not have prompted on this marketing version
        guard isVersionEligible else { return false }

        // Minimum 60 days between review prompts
        guard isDateEligible else { return false }

        return true
    }

    // MARK: - Actions

    func incrementScreenedCallCount() {
        screenedCallCount += 1
    }

    func recordAppointmentConfirmed() {
        appointmentConfirmed = true
    }

    /// Sync call count with server-reported call records if higher.
    func recordScreenedCalls(count: Int) {
        if count > screenedCallCount {
            screenedCallCount = count
        }
    }

    @discardableResult
    func requestReviewIfEligible(action: RequestReviewAction? = nil) -> Bool {
        guard isEligibleForReview else { return false }

        let now = currentDateProvider()
        let version = currentVersionProvider()

        lastPromptedDate = now
        lastPromptedVersion = version

        if let action = action {
            action()
        } else {
            reviewRequester()
        }

        return true
    }

    static func requestAppStoreReview() {
        #if DEBUG
        if AppStoreScreenshotFixtures.isEnabled { return }
        #endif

        if let scene = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .first(where: { $0.activationState == .foregroundActive }) {
            AppStore.requestReview(in: scene)
        } else if let scene = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .first {
            AppStore.requestReview(in: scene)
        }
    }

    func resetForTesting() {
        userDefaults.removeObject(forKey: Keys.screenedCallCount)
        userDefaults.removeObject(forKey: Keys.appointmentConfirmed)
        userDefaults.removeObject(forKey: Keys.lastPromptedDate)
        userDefaults.removeObject(forKey: Keys.lastPromptedVersion)
    }
}

// MARK: - Feedback & Support Helper

enum FeedbackSupport {
    static let recipient = "support@heykevin.one"
    static let fallbackURL = URL(string: "https://heykevin.one")!

    static func buildMailtoURL(
        version: String,
        build: String,
        iOSVersion: String,
        deviceModel: String,
        contractorId: String
    ) -> URL? {
        let subject = "Hey Kevin Feedback (\(version))"
        let body = "\n\n---\nApp Version: \(version) (\(build))\niOS: \(iOSVersion)\nDevice: \(deviceModel)\nAccount ID: \(contractorId)"

        var queryAllowed = CharacterSet.urlQueryAllowed
        queryAllowed.remove(charactersIn: "&+=?")

        let encodedSubject = subject.addingPercentEncoding(withAllowedCharacters: queryAllowed) ?? ""
        let encodedBody = body.addingPercentEncoding(withAllowedCharacters: queryAllowed) ?? ""

        return URL(string: "mailto:\(recipient)?subject=\(encodedSubject)&body=\(encodedBody)")
    }

    static func deviceModelIdentifier() -> String {
        #if targetEnvironment(simulator)
        if let simModel = ProcessInfo.processInfo.environment["SIMULATOR_MODEL_IDENTIFIER"], !simModel.isEmpty {
            return simModel
        }
        #endif

        var systemInfo = utsname()
        uname(&systemInfo)
        let machineMirror = Mirror(reflecting: systemInfo.machine)
        let identifier = machineMirror.children.reduce("") { identifier, element in
            guard let value = element.value as? Int8, value != 0 else { return identifier }
            return identifier + String(UnicodeScalar(UInt8(value)))
        }
        return identifier.isEmpty ? UIDevice.current.model : identifier
    }

    @MainActor
    static func sendFeedback(
        contractorId: String,
        canOpenURL: @MainActor (URL) -> Bool = { UIApplication.shared.canOpenURL($0) },
        openURL: @escaping @MainActor (URL) -> Void = { UIApplication.shared.open($0) }
    ) {
        let version = AppVersionService.marketingVersion()
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "0"
        let iOSVersion = UIDevice.current.systemVersion
        let model = deviceModelIdentifier()
        let id = contractorId.isEmpty ? "None" : contractorId

        guard let mailURL = buildMailtoURL(
            version: version,
            build: build,
            iOSVersion: iOSVersion,
            deviceModel: model,
            contractorId: id
        ), canOpenURL(mailURL) else {
            openURL(fallbackURL)
            return
        }

        openURL(mailURL)
    }
}
