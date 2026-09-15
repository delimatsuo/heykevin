import Foundation
import Combine
import SwiftUI

/// Filter tabs for screened call history.
enum CallHistoryFilter: String, CaseIterable, Identifiable, Sendable {
    case all = "all"
    case unread = "unread"
    case spam = "spam"

    var id: String { rawValue }

    var localizedTitle: String {
        switch self {
        case .all:    return String(localized: "All")
        case .unread: return String(localized: "Unread")
        case .spam:   return String(localized: "Spam")
        }
    }
}

/// Distinct empty outcome states for call history.
enum CallHistoryEmptyState: Equatable, Sendable {
    case noCalls
    case noUnread
    case noSpam
    case noMatches

    var title: String {
        switch self {
        case .noCalls:   return String(localized: "No screened calls yet")
        case .noUnread:  return String(localized: "No unread calls")
        case .noSpam:    return String(localized: "No spam calls")
        case .noMatches: return String(localized: "No matching calls")
        }
    }

    var description: String {
        switch self {
        case .noCalls:   return String(localized: "When someone calls, Kevin will screen and it will show here.")
        case .noUnread:  return String(localized: "You are all caught up on screened calls.")
        case .noSpam:    return String(localized: "No spam or blocked calls recorded.")
        case .noMatches: return String(localized: "Check the spelling or try a different phone number or keyword.")
        }
    }
}

/// A calendar-day group of call records for structured display.
struct CallDateGroup: Identifiable, Equatable {
    let id: String
    let title: String
    let calls: [CallRecord]
}

/// Production observable model owning bounded call history state and presentation policy.
@MainActor
final class CallHistoryModel: ObservableObject {
    nonisolated static let pageSize = 20
    nonisolated static let maximumCount = 100
    nonisolated static let retentionDays = 90

    typealias AuthProvider = @MainActor () -> CallAuthContext
    typealias FetchHandler = (CallAuthContext) async throws -> [CallRecord]
    typealias MarkReadHandler = ([String], CallAuthContext) async -> Void
    typealias LoadReadIdsEffect = @MainActor (CallAuthContext) -> Set<String>
    typealias CommitReadStateEffect = @MainActor (Set<String>, [CallRecord], CallAuthContext) -> Void
    typealias ResetEffect = @MainActor (CallAuthContext) -> Void
    typealias ReauthEffect = @MainActor (CallAuthContext) -> Void
    typealias Clock = () -> Date

    private let authProvider: AuthProvider
    private let fetchCalls: FetchHandler
    private let markServerRead: MarkReadHandler
    private let loadReadIds: LoadReadIdsEffect
    private let commitReadState: CommitReadStateEffect
    private let resetEffect: ResetEffect
    private let reauthEffect: ReauthEffect
    private let clock: Clock

    @Published private(set) var allCalls: [CallRecord] = []
    @Published var visibleLimit: Int = CallHistoryModel.pageSize
    @Published var selectedFilter: CallHistoryFilter = .all
    @Published var searchQuery: String = ""
    @Published private(set) var isLoading: Bool = false
    @Published private(set) var rawErrorMessage: String? = nil
    @Published private(set) var rawRetainedErrorMessage: String? = nil
    @Published private(set) var readCallIds: Set<String> = []
    @Published private(set) var activeAuthContext: CallAuthContext? = nil
    @Published private(set) var lastLoadedRevision: Int = 0

    /// Production getter confirming active auth context matches current auth provider and is valid.
    var hasOwnedSnapshot: Bool {
        guard let active = activeAuthContext, active.isValid else { return false }
        return active == authProvider()
    }

    var errorMessage: String? {
        guard hasOwnedSnapshot else { return nil }
        return rawErrorMessage
    }

    var retainedErrorMessage: String? {
        guard hasOwnedSnapshot else { return nil }
        return rawRetainedErrorMessage
    }

    private var currentRequestRevision: Int = 0

    init(
        authProvider: @escaping AuthProvider = { AppState.shared.currentAuthContext() },
        fetchCalls: @escaping FetchHandler = { auth in try await APIClient.shared.getCallHistory(authContext: auth) },
        markServerRead: @escaping MarkReadHandler = { sids, auth in await APIClient.shared.markCallsRead(sids, authContext: auth) },
        loadReadIds: @escaping LoadReadIdsEffect = { auth in CallHistoryModel.liveLoadReadIds(auth: auth) },
        commitReadState: @escaping CommitReadStateEffect = { ids, calls, auth in CallHistoryModel.liveCommitReadState(ids: ids, calls: calls, auth: auth) },
        resetEffect: @escaping ResetEffect = { auth in CallHistoryModel.liveReset(auth: auth) },
        reauthEffect: @escaping ReauthEffect = { auth in CallHistoryModel.liveReauth(auth: auth) },
        clock: @escaping Clock = { Date() }
    ) {
        self.authProvider = authProvider
        self.fetchCalls = fetchCalls
        self.markServerRead = markServerRead
        self.loadReadIds = loadReadIds
        self.commitReadState = commitReadState
        self.resetEffect = resetEffect
        self.reauthEffect = reauthEffect
        self.clock = clock
        let auth = authProvider()
        self.activeAuthContext = auth.isValid ? auth : nil
        self.readCallIds = []
    }

    // MARK: - Live Effects (Production)

    #if DEBUG
    private static var isScreenshotFixture: Bool {
        ProcessInfo.processInfo.environment["APP_STORE_SCREENSHOT_SCENARIO"] != nil
    }
    #else
    private static var isScreenshotFixture: Bool { false }
    #endif

    private static func liveLoadReadIds(auth: CallAuthContext) -> Set<String> {
        guard auth.isValid, !auth.contractorId.isEmpty else { return [] }
        guard AppState.shared.currentAuthContext() == auth else { return [] }
        if isScreenshotFixture { return [] }
        let arr = UserDefaults.standard.stringArray(forKey: "readCallIds_\(auth.contractorId)") ?? []
        return Set(arr)
    }

    private static func liveCommitReadState(ids: Set<String>, calls: [CallRecord], auth: CallAuthContext) {
        guard auth.isValid, !auth.contractorId.isEmpty else { return }
        guard AppState.shared.currentAuthContext() == auth else { return }
        if !isScreenshotFixture {
            UserDefaults.standard.set(Array(ids), forKey: "readCallIds_\(auth.contractorId)")
        }
        AppState.shared.readCallIds = ids
        AppState.shared.updateUnreadCount(calls: calls)
    }

    private static func liveReset(auth: CallAuthContext) {
        guard AppState.shared.currentAuthContext() == auth else { return }
        guard auth.isValid, !auth.contractorId.isEmpty else {
            AppState.shared.readCallIds = []
            AppState.shared.unreadCallCount = 0
            AppState.shared.notificationCallSid = ""
            AppState.shared.notificationCallMessage = ""
            return
        }
        AppState.shared.readCallIds = liveLoadReadIds(auth: auth)
        AppState.shared.unreadCallCount = 0
        AppState.shared.notificationCallSid = ""
        AppState.shared.notificationCallMessage = ""
    }

    private static func liveReauth(auth: CallAuthContext) {
        guard auth.isValid, !auth.contractorId.isEmpty else { return }
        guard AppState.shared.currentAuthContext() == auth else { return }
        AppState.shared.needsReauth = true
    }

    // MARK: - Normalization and Bounded Retention Policy

    /// Pure policy function normalizing raw records:
    /// 1. Discard invalid or empty IDs and invalid timestamps
    /// 2. Exclude timestamps older than 90 days
    /// 3. Deduplicate by ID: newest timestamp wins (equal timestamp duplicates keep first source occurrence)
    /// 4. Sort timestamp descending, then ID ascending
    /// 5. Cap at 100 most recent records
    static func normalize(
        rawCalls: [CallRecord],
        now: Date = Date(),
        retentionDays: Int = retentionDays,
        maxCount: Int = maximumCount
    ) -> [CallRecord] {
        let cutoff = now.addingTimeInterval(-Double(retentionDays * 86400))
        var deduplicated: [String: (record: CallRecord, index: Int)] = [:]

        for (index, record) in rawCalls.enumerated() {
            let trimmedId = record.id.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmedId.isEmpty else { continue }
            let ts = record.timestamp.timeIntervalSince1970
            guard ts.isFinite, !ts.isNaN, ts > 0 else { continue }
            guard record.timestamp >= cutoff else { continue }

            if let existing = deduplicated[trimmedId] {
                if record.timestamp > existing.record.timestamp {
                    deduplicated[trimmedId] = (record, index)
                } else if record.timestamp == existing.record.timestamp {
                    // Equal timestamp: first source occurrence wins (keep existing)
                } else {
                    // Older timestamp: keep existing
                }
            } else {
                deduplicated[trimmedId] = (record, index)
            }
        }

        let sorted = deduplicated.values.map { $0.record }.sorted { a, b in
            if a.timestamp != b.timestamp {
                return a.timestamp > b.timestamp
            }
            return a.id < b.id
        }

        return Array(sorted.prefix(maxCount))
    }

    // MARK: - Search and Filtering (Applied before visible limit)

    var filteredCalls: [CallRecord] {
        guard hasOwnedSnapshot else { return [] }
        let callsToFilter: [CallRecord]
        switch selectedFilter {
        case .all:
            callsToFilter = allCalls
        case .unread:
            callsToFilter = allCalls.filter { isCallUnread($0) }
        case .spam:
            callsToFilter = allCalls.filter { $0.isSpamOrBlocked }
        }

        let query = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else {
            return callsToFilter
        }

        let isPhoneLikeQuery = query.allSatisfy { char in
            char.isNumber || "+()-./ \t".contains(char)
        }
        let queryDigits = query.filter { $0.isNumber }

        return callsToFilter.filter { call in
            // 1. Caller Name search (case- and diacritic-insensitive)
            if call.callerName.range(of: query, options: [.caseInsensitive, .diacriticInsensitive]) != nil {
                return true
            }
            // 2. Caller Phone & Formatted Phone substring match
            if call.callerPhone.range(of: query, options: [.caseInsensitive]) != nil ||
               call.formattedPhone.range(of: query, options: [.caseInsensitive]) != nil {
                return true
            }
            if let cb = call.callbackNumber, cb.range(of: query, options: [.caseInsensitive]) != nil {
                return true
            }
            // 3. Normalized Phone Digits match (ONLY when query consists of phone digits and punctuation)
            if isPhoneLikeQuery && !queryDigits.isEmpty {
                if call.normalizedPhoneDigits.contains(queryDigits) {
                    return true
                }
                if let cb = call.callbackNumber?.filter({ $0.isNumber }), cb.contains(queryDigits) {
                    return true
                }
            }
            // 4. Exact Caller Excerpt search (case- and diacritic-insensitive)
            if call.callerExcerpt.range(of: query, options: [.caseInsensitive, .diacriticInsensitive]) != nil {
                return true
            }
            return false
        }
    }

    var visibleCalls: [CallRecord] {
        Array(filteredCalls.prefix(visibleLimit))
    }

    var canShowMore: Bool {
        hasOwnedSnapshot && visibleLimit < filteredCalls.count && visibleLimit < Self.maximumCount
    }

    var hasCalls: Bool {
        hasOwnedSnapshot && !allCalls.isEmpty
    }

    var unreadCount: Int {
        guard hasOwnedSnapshot else { return 0 }
        return allCalls.filter { isCallUnread($0) }.count
    }

    // MARK: - Read State Management

    func isCallUnread(_ call: CallRecord) -> Bool {
        if call.readOnServer { return false }
        return call.hasMessage && !readCallIds.contains(call.id)
    }

    @discardableResult
    func markAsRead(callId: String, expectedAuth: CallAuthContext? = nil) -> Task<Void, Never>? {
        let current = authProvider()
        guard current.isValid else { return nil }
        if let expected = expectedAuth, expected != current { return nil }
        guard let active = activeAuthContext, active == current else { return nil }
        guard allCalls.contains(where: { $0.id == callId }) else { return nil }

        readCallIds.insert(callId)
        commitReadState(readCallIds, allCalls, current)

        let task = Task { [markServerRead, current, weak self] in
            guard let self = self else { return }
            let revalidated = self.authProvider()
            guard revalidated == current, revalidated.isValid else { return }
            await markServerRead([callId], current)
        }
        return task
    }

    /// Factory creating an action closure that captures expected auth at render time.
    /// Rejects execution if expected auth is absent, invalid, or mismatched at invocation time.
    func makeMarkAllReadAction(expectedAuth: CallAuthContext?) -> () -> Void {
        guard let expected = expectedAuth, expected.isValid else {
            return {}
        }
        return { [weak self] in
            _ = self?.markAllAsRead(expectedAuth: expected)
        }
    }

    @discardableResult
    func markAllAsRead(expectedAuth: CallAuthContext? = nil) -> Task<Void, Never>? {
        let current = authProvider()
        guard current.isValid else { return nil }
        if let expected = expectedAuth, expected != current { return nil }
        guard let active = activeAuthContext, active == current else { return nil }

        let unreadIds = allCalls.filter { isCallUnread($0) }.map { $0.id }
        guard !unreadIds.isEmpty else { return nil }

        readCallIds.formUnion(unreadIds)
        commitReadState(readCallIds, allCalls, current)

        let task = Task { [markServerRead, current, weak self] in
            guard let self = self else { return }
            let revalidated = self.authProvider()
            guard revalidated == current, revalidated.isValid else { return }
            await markServerRead(unreadIds, current)
        }
        return task
    }

    // MARK: - Appointment Confirmation Receipt

    /// Updates local screened call history with a confirmed appointment receipt.
    /// Replaces the target CallRecord with a confirmed copy preserving all other fields,
    /// increments request revision to invalidate older in-flight fetches, and settles loading state.
    @discardableResult
    func applyAppointmentConfirmation(
        callId: String,
        callerNotified: Bool,
        expectedAuth: CallAuthContext?
    ) -> Bool {
        guard let expected = expectedAuth, expected.isValid else { return false }
        let current = authProvider()
        guard current.isValid, expected == current else { return false }
        guard let active = activeAuthContext, active == current else { return false }
        guard let index = allCalls.firstIndex(where: { $0.id == callId }) else { return false }

        let existing = allCalls[index]
        let confirmed = CallRecord(
            id: existing.id,
            callerPhone: existing.callerPhone,
            callerName: existing.callerName,
            timestamp: existing.timestamp,
            trustScore: existing.trustScore,
            outcome: existing.outcome,
            transcript: existing.transcript,
            voicemailURL: existing.voicemailURL,
            callbackNumber: existing.callbackNumber,
            readOnServer: existing.readOnServer,
            appointmentStatus: "confirmed",
            appointmentStartTime: existing.appointmentStartTime,
            appointmentTitle: existing.appointmentTitle,
            appointmentCallerNotified: callerNotified
        )

        allCalls[index] = confirmed
        currentRequestRevision += 1
        isLoading = false
        return true
    }

    // MARK: - Loading, Guards, and Invalidation

    func loadCalls() async {
        let requestAuth = authProvider()
        guard requestAuth.isValid else {
            invalidate()
            return
        }

        if activeAuthContext != requestAuth {
            invalidate(for: requestAuth)
        }

        currentRequestRevision += 1
        let capturedRevision = currentRequestRevision
        isLoading = true
        rawErrorMessage = nil
        rawRetainedErrorMessage = nil

        do {
            let raw = try await fetchCalls(requestAuth)

            // GUARD: Must match both captured revision AND current auth context
            guard capturedRevision == self.currentRequestRevision,
                  self.authProvider() == requestAuth,
                  requestAuth.isValid else {
                return
            }

            let normalized = Self.normalize(
                rawCalls: raw,
                now: self.clock(),
                retentionDays: Self.retentionDays,
                maxCount: Self.maximumCount
            )

            self.allCalls = normalized
            self.activeAuthContext = requestAuth
            self.lastLoadedRevision = capturedRevision

            // Load scoped read IDs only into owned load
            var loadedIds = self.loadReadIds(requestAuth)
            loadedIds.formUnion(self.readCallIds)
            for r in normalized where r.readOnServer {
                loadedIds.insert(r.id)
            }
            let validIds = Set(normalized.map { $0.id })
            self.readCallIds = loadedIds.intersection(validIds)
            self.commitReadState(self.readCallIds, self.allCalls, requestAuth)

            // Preserve expansion bounded to valid limits
            if self.visibleLimit < Self.pageSize {
                self.visibleLimit = Self.pageSize
            } else if self.visibleLimit > Self.maximumCount {
                self.visibleLimit = Self.maximumCount
            }

            self.isLoading = false
            self.rawErrorMessage = nil
            self.rawRetainedErrorMessage = nil
        } catch {
            // GUARD: Must match both captured revision AND current auth context
            guard capturedRevision == self.currentRequestRevision,
                  self.authProvider() == requestAuth,
                  requestAuth.isValid else {
                return
            }

            if let histErr = error as? CallHistoryError, histErr == .unauthorized {
                self.reauthEffect(requestAuth)
            }

            self.activeAuthContext = requestAuth
            if !self.allCalls.isEmpty {
                self.rawRetainedErrorMessage = error.localizedDescription
            } else {
                self.rawErrorMessage = error.localizedDescription
            }
            self.isLoading = false
        }
    }

    func invalidate(for newAuth: CallAuthContext? = nil) {
        currentRequestRevision += 1
        allCalls = []
        visibleLimit = Self.pageSize
        selectedFilter = .all
        searchQuery = ""
        isLoading = false
        rawErrorMessage = nil
        rawRetainedErrorMessage = nil
        readCallIds = []
        let targetAuth = newAuth ?? authProvider()
        activeAuthContext = targetAuth.isValid ? targetAuth : nil
        resetEffect(targetAuth)
    }

    // MARK: - Presentation Controls

    func showMore() {
        visibleLimit = min(visibleLimit + Self.pageSize, Self.maximumCount)
    }

    func setSearchQuery(_ query: String) {
        searchQuery = query
        visibleLimit = Self.pageSize
    }

    func setFilter(_ filter: CallHistoryFilter) {
        selectedFilter = filter
        visibleLimit = Self.pageSize
    }

    func clearSearch() {
        searchQuery = ""
        visibleLimit = Self.pageSize
    }

    // MARK: - Labels and Copy

    var showingCountLabel: String {
        guard hasOwnedSnapshot else { return "" }
        let total = filteredCalls.count
        guard total > 0 else { return "" }
        if total >= Self.maximumCount && visibleCalls.count >= Self.maximumCount && selectedFilter == .all && searchQuery.isEmpty {
            return String(localized: "Showing the 100 most recent calls available in this history.")
        }
        return String(localized: "Showing \(visibleCalls.count) of \(total) calls")
    }

    var historyFootnoteLabel: String {
        String(localized: "History includes up to 100 recent calls from the last 90 days.")
    }

    var emptyState: CallHistoryEmptyState? {
        guard hasOwnedSnapshot && !isLoading && errorMessage == nil && filteredCalls.isEmpty else { return nil }
        if !searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .noMatches
        }
        switch selectedFilter {
        case .all:
            return .noCalls
        case .unread:
            return .noUnread
        case .spam:
            return .noSpam
        }
    }

    func call(for id: String) -> CallRecord? {
        guard hasOwnedSnapshot else { return nil }
        return allCalls.first { $0.id == id }
    }

    var groupedVisibleCalls: [CallDateGroup] {
        guard hasOwnedSnapshot else { return [] }
        let calendar = Calendar.current
        let now = clock()
        var groups: [String: (title: String, orderDate: Date, calls: [CallRecord])] = [:]

        for call in visibleCalls {
            let date = call.timestamp
            let key: String
            let title: String
            if calendar.isDate(date, inSameDayAs: now) {
                key = "0_today"
                title = String(localized: "Today")
            } else if let yesterday = calendar.date(byAdding: .day, value: -1, to: now),
                      calendar.isDate(date, inSameDayAs: yesterday) {
                key = "1_yesterday"
                title = String(localized: "Yesterday")
            } else {
                let startOfDay = calendar.startOfDay(for: date)
                key = "2_\(startOfDay.timeIntervalSince1970)"
                title = date.formatted(date: .abbreviated, time: .omitted)
            }

            if var group = groups[key] {
                group.calls.append(call)
                groups[key] = group
            } else {
                let startOfDay = calendar.startOfDay(for: date)
                groups[key] = (title: title, orderDate: startOfDay, calls: [call])
            }
        }

        return groups.sorted { a, b in
            if a.value.orderDate != b.value.orderDate {
                return a.value.orderDate > b.value.orderDate
            }
            return a.key < b.key
        }.map { key, value in
            CallDateGroup(id: key, title: value.title, calls: value.calls)
        }
    }
}
