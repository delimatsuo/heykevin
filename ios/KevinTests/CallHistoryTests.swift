import XCTest
@testable import Kevin

@MainActor
final class CallHistoryTests: XCTestCase {

    // MARK: - In-Memory Isolated Test Effects

    private final class MockHistoryEffects {
        var storedReadIds: [String: Set<String>] = [:]
        var committedReadState: [(ids: Set<String>, calls: [CallRecord], auth: CallAuthContext)] = []
        var resetAuths: [CallAuthContext] = []
        var reauthAuths: [CallAuthContext] = []
        var serverMarkedSids: [(sids: [String], auth: CallAuthContext)] = []

        func loadReadIds(auth: CallAuthContext) -> Set<String> {
            storedReadIds[auth.contractorId] ?? []
        }

        func commitReadState(ids: Set<String>, calls: [CallRecord], auth: CallAuthContext) {
            storedReadIds[auth.contractorId] = ids
            committedReadState.append((ids: ids, calls: calls, auth: auth))
        }

        func reset(auth: CallAuthContext) {
            resetAuths.append(auth)
        }

        func reauth(auth: CallAuthContext) {
            reauthAuths.append(auth)
        }

        func markServerRead(sids: [String], auth: CallAuthContext) async {
            serverMarkedSids.append((sids: sids, auth: auth))
        }
    }

    private func makeTestModel(
        authProvider: @escaping () -> CallAuthContext,
        fetchCalls: @escaping CallHistoryModel.FetchHandler,
        effects: MockHistoryEffects = MockHistoryEffects(),
        clock: @escaping () -> Date = { Date() }
    ) -> (model: CallHistoryModel, effects: MockHistoryEffects) {
        let model = CallHistoryModel(
            authProvider: authProvider,
            fetchCalls: fetchCalls,
            markServerRead: { sids, auth in await effects.markServerRead(sids: sids, auth: auth) },
            loadReadIds: { auth in effects.loadReadIds(auth: auth) },
            commitReadState: { ids, calls, auth in effects.commitReadState(ids: ids, calls: calls, auth: auth) },
            resetEffect: { auth in effects.reset(auth: auth) },
            reauthEffect: { auth in effects.reauth(auth: auth) },
            clock: clock
        )
        return (model, effects)
    }

    // MARK: - Test Helpers & Fixtures

    private func makeRecord(
        id: String,
        name: String = "Test Caller",
        phone: String = "5551234567",
        timestamp: Date,
        outcome: String = "screened",
        transcript: String = "Kevin: Hello\nCaller: Hi there, I need help with my kitchen sink.\nCaller: Please give me a call back.",
        readOnServer: Bool = false,
        appointmentStatus: String? = nil,
        appointmentStartTime: String? = nil,
        appointmentTitle: String? = nil,
        appointmentCallerNotified: Bool = false
    ) -> CallRecord {
        CallRecord(
            id: id,
            callerPhone: phone,
            callerName: name,
            timestamp: timestamp,
            trustScore: 80,
            outcome: outcome,
            transcript: transcript,
            voicemailURL: nil,
            callbackNumber: nil,
            readOnServer: readOnServer,
            appointmentStatus: appointmentStatus,
            appointmentStartTime: appointmentStartTime,
            appointmentTitle: appointmentTitle,
            appointmentCallerNotified: appointmentCallerNotified
        )
    }

    private func makeHTTPResponse(statusCode: Int) -> HTTPURLResponse {
        HTTPURLResponse(
            url: URL(string: "https://kevin.test/api/calls")!,
            statusCode: statusCode,
            httpVersion: nil,
            headerFields: nil
        )!
    }

    // MARK: - 1. Decoder Tests (CallHistoryResponseParser & CallHistoryError)

    func testDecoderValidEmptyCallsArray() throws {
        let json = #"{"status":"ok","calls":[]}"#
        let data = json.data(using: .utf8)!
        let response = makeHTTPResponse(statusCode: 200)

        let records = try CallHistoryResponseParser.parse(data: data, response: response)
        XCTAssertTrue(records.isEmpty)
    }

    func testDecoderValidCallsWithFullPayload() throws {
        let json = """
        {
            "status": "ok",
            "calls": [
                {
                    "call_sid": "CA1234567890",
                    "caller_phone": "+15551234567",
                    "caller_name": "Alice Smith",
                    "timestamp": 1773532800,
                    "trust_score": 95,
                    "outcome": "screened",
                    "transcript": "Kevin: Hello\\nCaller: I want an appointment for tomorrow morning.\\nCaller: Please call me back.",
                    "voicemail_url": "https://example.com/vm.mp3",
                    "callback_number": "+15559876543",
                    "read": true,
                    "appointment_request": {
                        "status": "confirmed",
                        "start_time": "2026-09-15T09:00:00Z",
                        "title": "Plumbing Inspection",
                        "caller_notified_at": "2026-09-14T12:00:00Z"
                    }
                }
            ]
        }
        """
        let data = json.data(using: .utf8)!
        let response = makeHTTPResponse(statusCode: 200)

        let records = try CallHistoryResponseParser.parse(data: data, response: response)
        XCTAssertEqual(records.count, 1)

        let record = records[0]
        XCTAssertEqual(record.id, "CA1234567890")
        XCTAssertEqual(record.callerPhone, "+15551234567")
        XCTAssertEqual(record.callerName, "Alice Smith")
        XCTAssertEqual(record.timestamp, Date(timeIntervalSince1970: 1773532800))
        XCTAssertEqual(record.trustScore, 95)
        XCTAssertEqual(record.outcome, "screened")
        XCTAssertEqual(record.voicemailURL, "https://example.com/vm.mp3")
        XCTAssertEqual(record.callbackNumber, "+15559876543")
        XCTAssertTrue(record.readOnServer)
        XCTAssertEqual(record.appointmentStatus, "confirmed")
        XCTAssertEqual(record.appointmentStartTime, "2026-09-15T09:00:00Z")
        XCTAssertEqual(record.appointmentTitle, "Plumbing Inspection")
        XCTAssertTrue(record.appointmentCallerNotified)
        XCTAssertTrue(record.hasMessage)
    }

    func testDecoderThrowsForMissingOrEmptyCallSid() {
        let testCases = [
            #"{"calls":[{"call_sid":"","timestamp":1773532800}]}"#,
            #"{"calls":[{"call_sid":"   ","timestamp":1773532800}]}"#,
            #"{"calls":[{"timestamp":1773532800}]}"#,
            #"{"calls":[{"call_sid":12345,"timestamp":1773532800}]}"#
        ]

        let response = makeHTTPResponse(statusCode: 200)
        for json in testCases {
            let data = json.data(using: .utf8)!
            XCTAssertThrowsError(try CallHistoryResponseParser.parse(data: data, response: response)) { error in
                XCTAssertEqual(error as? CallHistoryError, .malformedResponse)
            }
        }
    }

    func testDecoderThrowsForInvalidTimestamps() {
        let testCases = [
            #"{"calls":[{"call_sid":"CA1","timestamp":0}]}"#,
            #"{"calls":[{"call_sid":"CA2","timestamp":-100}]}"#,
            #"{"calls":[{"call_sid":"CA3"}]}"#,
            #"{"calls":[{"call_sid":"CA4","timestamp":true}]}"#,
            #"{"calls":[{"call_sid":"CA5","timestamp":false}]}"#,
            #"{"calls":[{"call_sid":"CA6","timestamp":"1773532800"}]}"#
        ]

        let response = makeHTTPResponse(statusCode: 200)
        for json in testCases {
            let data = json.data(using: .utf8)!
            XCTAssertThrowsError(try CallHistoryResponseParser.parse(data: data, response: response)) { error in
                XCTAssertEqual(error as? CallHistoryError, .malformedResponse)
            }
        }
    }

    func testDecoderThrowsForMixtureOfValidAndInvalidRecords() {
        let json = """
        {
            "calls": [
                {
                    "call_sid": "CA_VALID_1",
                    "timestamp": 1773532800
                },
                {
                    "call_sid": "CA_INVALID_NO_TIMESTAMP"
                }
            ]
        }
        """
        let data = json.data(using: .utf8)!
        let response = makeHTTPResponse(statusCode: 200)

        XCTAssertThrowsError(try CallHistoryResponseParser.parse(data: data, response: response)) { error in
            XCTAssertEqual(error as? CallHistoryError, .malformedResponse)
        }
    }

    func testDecoderMalformedJSONThrowsMalformedResponse() {
        let invalidData = "This is not JSON".data(using: .utf8)!
        let response = makeHTTPResponse(statusCode: 200)

        XCTAssertThrowsError(try CallHistoryResponseParser.parse(data: invalidData, response: response)) { error in
            XCTAssertEqual(error as? CallHistoryError, .malformedResponse)
        }
    }

    func testDecoderMissingCallsEnvelopeThrowsMalformedResponse() {
        let missingEnvelope = #"{"status":"ok","items":[]}"#.data(using: .utf8)!
        let response = makeHTTPResponse(statusCode: 200)

        XCTAssertThrowsError(try CallHistoryResponseParser.parse(data: missingEnvelope, response: response)) { error in
            XCTAssertEqual(error as? CallHistoryError, .malformedResponse)
        }
    }

    func testDecoderHTTP401ThrowsUnauthorized() {
        let data = #"{"error":"Unauthorized"}"#.data(using: .utf8)!
        let response = makeHTTPResponse(statusCode: 401)

        XCTAssertThrowsError(try CallHistoryResponseParser.parse(data: data, response: response)) { error in
            XCTAssertEqual(error as? CallHistoryError, .unauthorized)
        }
    }

    func testDecoderHTTP403ThrowsForbidden() {
        let data = #"{"error":"Forbidden"}"#.data(using: .utf8)!
        let response = makeHTTPResponse(statusCode: 403)

        XCTAssertThrowsError(try CallHistoryResponseParser.parse(data: data, response: response)) { error in
            XCTAssertEqual(error as? CallHistoryError, .forbidden)
        }
    }

    func testDecoderHTTP5xxThrowsServerError() {
        for code in [500, 502, 503, 504] {
            let data = #"{"error":"Server error"}"#.data(using: .utf8)!
            let response = makeHTTPResponse(statusCode: code)

            XCTAssertThrowsError(try CallHistoryResponseParser.parse(data: data, response: response)) { error in
                XCTAssertEqual(error as? CallHistoryError, .serverError(statusCode: code))
            }
        }
    }

    func testDecoderHTTP4xxThrowsHttpError() {
        let data = #"{"error":"Not found"}"#.data(using: .utf8)!
        let response = makeHTTPResponse(statusCode: 404)

        XCTAssertThrowsError(try CallHistoryResponseParser.parse(data: data, response: response)) { error in
            XCTAssertEqual(error as? CallHistoryError, .httpError(statusCode: 404))
        }
    }

    func testDecoderHasNoAppStateSideEffects() throws {
        let initialReadIds = AppState.shared.readCallIds
        let json = """
        {
            "calls": [
                {
                    "call_sid": "CA_SIDE_EFFECT_TEST",
                    "timestamp": 1773532800,
                    "read": true
                }
            ]
        }
        """
        let data = json.data(using: .utf8)!
        let response = makeHTTPResponse(statusCode: 200)

        _ = try CallHistoryResponseParser.parse(data: data, response: response)
        XCTAssertEqual(AppState.shared.readCallIds, initialReadIds)
    }

    // MARK: - 2. Normalization & Bounded Policy Tests

    func testNormalizeCap100() {
        let now = Date(timeIntervalSince1970: 1773532800)
        var raw: [CallRecord] = []
        for i in 0..<110 {
            raw.append(makeRecord(
                id: "call-\(i)",
                timestamp: now.addingTimeInterval(-Double(i * 100))
            ))
        }

        let normalized = CallHistoryModel.normalize(rawCalls: raw, now: now)
        XCTAssertEqual(normalized.count, 100)
        XCTAssertEqual(normalized.first?.id, "call-0")
        XCTAssertEqual(normalized.last?.id, "call-99")
    }

    func testNormalize90DayRetentionBoundary() {
        let now = Date(timeIntervalSince1970: 1773532800)
        let daySeconds: Double = 86400

        let call89Days = makeRecord(id: "call-89", timestamp: now.addingTimeInterval(-89 * daySeconds))
        let call90Days = makeRecord(id: "call-90", timestamp: now.addingTimeInterval(-90 * daySeconds))
        let call91Days = makeRecord(id: "call-91", timestamp: now.addingTimeInterval(-90 * daySeconds - 10))

        let normalized = CallHistoryModel.normalize(
            rawCalls: [call89Days, call90Days, call91Days],
            now: now
        )

        let ids = normalized.map { $0.id }
        XCTAssertTrue(ids.contains("call-89"))
        XCTAssertTrue(ids.contains("call-90"))
        XCTAssertFalse(ids.contains("call-91"))
    }

    func testNormalizeDeduplicationNewerTimestampWins() {
        let now = Date(timeIntervalSince1970: 1773532800)
        let older = makeRecord(id: "call-dup", name: "Older Version", timestamp: now.addingTimeInterval(-1000))
        let newer = makeRecord(id: "call-dup", name: "Newer Version", timestamp: now.addingTimeInterval(-500))

        let normalized = CallHistoryModel.normalize(rawCalls: [older, newer], now: now)
        XCTAssertEqual(normalized.count, 1)
        XCTAssertEqual(normalized[0].callerName, "Newer Version")
        XCTAssertEqual(normalized[0].timestamp, now.addingTimeInterval(-500))
    }

    func testNormalizeDeduplicationEqualTimestampKeepsFirstSourceOccurrence() {
        let now = Date(timeIntervalSince1970: 1773532800)
        let sameTime = now.addingTimeInterval(-500)
        let first = makeRecord(id: "call-equal", name: "First Occurrence", timestamp: sameTime)
        let second = makeRecord(id: "call-equal", name: "Second Occurrence", timestamp: sameTime)

        let normalized = CallHistoryModel.normalize(rawCalls: [first, second], now: now)
        XCTAssertEqual(normalized.count, 1)
        XCTAssertEqual(normalized[0].callerName, "First Occurrence")
    }

    func testNormalizeSortingTimestampDescThenIdAsc() {
        let now = Date(timeIntervalSince1970: 1773532800)
        let t1 = now.addingTimeInterval(-100)
        let t2 = now.addingTimeInterval(-200)

        let callA_t2 = makeRecord(id: "call-A", timestamp: t2)
        let callB_t1 = makeRecord(id: "call-B", timestamp: t1)
        let callA_t1 = makeRecord(id: "call-A", timestamp: t1)
        let callC_t1 = makeRecord(id: "call-C", timestamp: t1)

        let normalized = CallHistoryModel.normalize(rawCalls: [callA_t2, callB_t1, callA_t1, callC_t1], now: now)

        XCTAssertEqual(normalized.map { $0.id }, ["call-A", "call-B", "call-C"])
    }

    // MARK: - 3. History Store, Auth Lifecycle & Injected Effects Tests

    func testOutOfOrderResponsesLatestRevisionWins() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        var continuation1: CheckedContinuation<[CallRecord], Error>?
        var continuation2: CheckedContinuation<[CallRecord], Error>?

        var fetchCount = 0
        let (model, effects) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in
                fetchCount += 1
                let callNumber = fetchCount
                return try await withCheckedThrowingContinuation { continuation in
                    if callNumber == 1 {
                        continuation1 = continuation
                    } else if callNumber == 2 {
                        continuation2 = continuation
                    }
                }
            },
            clock: { now }
        )

        // Launch Request 1
        let task1 = Task { await model.loadCalls() }
        while continuation1 == nil { await Task.yield() }

        // Launch Request 2
        let task2 = Task { await model.loadCalls() }
        while continuation2 == nil { await Task.yield() }

        // Resume Request 2 FIRST with fresh data
        let freshCalls = [self.makeRecord(id: "call-fresh", timestamp: now)]
        continuation2?.resume(returning: freshCalls)
        await task2.value

        XCTAssertEqual(model.allCalls.map { $0.id }, ["call-fresh"])
        XCTAssertFalse(model.isLoading)
        XCTAssertEqual(effects.committedReadState.last?.calls.map { $0.id }, ["call-fresh"])

        // Resume Request 1 SECOND with stale data
        let staleCalls = [self.makeRecord(id: "call-stale", timestamp: now.addingTimeInterval(-10))]
        continuation1?.resume(returning: staleCalls)
        await task1.value

        // Model MUST still retain fresh calls from Request 2
        XCTAssertEqual(model.allCalls.map { $0.id }, ["call-fresh"])
    }

    func testStaleErrorAfterSuccessIsDiscarded() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        var continuation1: CheckedContinuation<[CallRecord], Error>?
        var continuation2: CheckedContinuation<[CallRecord], Error>?

        var fetchCount = 0
        let (model, _) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in
                fetchCount += 1
                let callNumber = fetchCount
                return try await withCheckedThrowingContinuation { continuation in
                    if callNumber == 1 {
                        continuation1 = continuation
                    } else if callNumber == 2 {
                        continuation2 = continuation
                    }
                }
            },
            clock: { now }
        )

        let task1 = Task { await model.loadCalls() }
        while continuation1 == nil { await Task.yield() }

        let task2 = Task { await model.loadCalls() }
        while continuation2 == nil { await Task.yield() }

        // Success 2 finishes first
        let freshCalls = [self.makeRecord(id: "call-success", timestamp: now)]
        continuation2?.resume(returning: freshCalls)
        await task2.value

        XCTAssertEqual(model.allCalls.map { $0.id }, ["call-success"])
        XCTAssertNil(model.errorMessage)

        // Stale Error 1 finishes later
        continuation1?.resume(throwing: CallHistoryError.serverError(statusCode: 500))
        await task1.value

        // Stale error must be discarded: rows remain and error message is not overwritten
        XCTAssertEqual(model.allCalls.map { $0.id }, ["call-success"])
        XCTAssertNil(model.errorMessage)
        XCTAssertFalse(model.isLoading)
    }

    func testAuthChangeInvalidatesStoreAndClearsEffects() async {
        var auth = CallAuthContext(contractorId: "contractor-A", bearerToken: "token-A", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        let (model, effects) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in [self.makeRecord(id: "call-A", timestamp: now)] },
            clock: { now }
        )

        await model.loadCalls()
        XCTAssertEqual(model.allCalls.count, 1)

        model.setSearchQuery("search term")
        model.setFilter(.unread)
        model.showMore()

        // Invalidate for new contractor
        auth = CallAuthContext(contractorId: "contractor-B", bearerToken: "token-B", generation: 2)
        model.invalidate(for: auth)

        XCTAssertTrue(model.allCalls.isEmpty)
        XCTAssertEqual(model.searchQuery, "")
        XCTAssertEqual(model.selectedFilter, .all)
        XCTAssertEqual(model.visibleLimit, CallHistoryModel.pageSize)
        XCTAssertNil(model.errorMessage)
        XCTAssertEqual(model.unreadCount, 0)
        XCTAssertEqual(effects.resetAuths.last, auth)
    }

    func testABAAuthRotationDiscardsStaleResponse() async {
        var auth = CallAuthContext(contractorId: "contractor-A", bearerToken: "token-A1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        var continuation: CheckedContinuation<[CallRecord], Error>?
        let (model, _) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in
                try await withCheckedThrowingContinuation { cont in
                    continuation = cont
                }
            },
            clock: { now }
        )

        let task = Task { await model.loadCalls() }
        while continuation == nil { await Task.yield() }

        // A -> B -> A transition (same contractor name, but rotated generation)
        auth = CallAuthContext(contractorId: "contractor-B", bearerToken: "token-B", generation: 2)
        auth = CallAuthContext(contractorId: "contractor-A", bearerToken: "token-A2", generation: 3)

        continuation?.resume(returning: [self.makeRecord(id: "call-stale-generation", timestamp: now)])
        await task.value

        // Stale generation response is ignored
        XCTAssertTrue(model.allCalls.isEmpty)
    }

    func testHeldResponseStale401HasZeroEffects() async {
        var auth = CallAuthContext(contractorId: "contractor-A", bearerToken: "token-A1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        var continuation: CheckedContinuation<[CallRecord], Error>?
        let (model, effects) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in
                try await withCheckedThrowingContinuation { cont in
                    continuation = cont
                }
            },
            clock: { now }
        )

        let task = Task { await model.loadCalls() }
        while continuation == nil { await Task.yield() }

        // Auth rotates before response completes
        auth = CallAuthContext(contractorId: "contractor-B", bearerToken: "token-B", generation: 2)

        // Stale request returns 401
        continuation?.resume(throwing: CallHistoryError.unauthorized)
        await task.value

        // Prove ZERO reauth, ZERO read/persist, ZERO badge side effects for stale auth
        XCTAssertTrue(effects.reauthAuths.isEmpty)
        XCTAssertTrue(effects.committedReadState.isEmpty)
        XCTAssertNil(model.errorMessage)
    }

    func testRetainedErrorOnReload() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        var shouldFail = false
        let (model, _) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in
                if shouldFail {
                    throw CallHistoryError.serverError(statusCode: 503)
                }
                return [self.makeRecord(id: "call-1", timestamp: now)]
            },
            clock: { now }
        )

        await model.loadCalls()
        XCTAssertEqual(model.allCalls.count, 1)
        XCTAssertNil(model.errorMessage)
        XCTAssertNil(model.retainedErrorMessage)

        // Reload with failure
        shouldFail = true
        await model.loadCalls()

        // Calls are retained and retainedErrorMessage is populated
        XCTAssertEqual(model.allCalls.count, 1)
        XCTAssertNil(model.errorMessage)
        XCTAssertNotNil(model.retainedErrorMessage)
        XCTAssertFalse(model.isLoading)
    }

    func testServerReadFlagHydrationAndPruning() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        let calls = [
            makeRecord(id: "call-read-server", timestamp: now, readOnServer: true),
            makeRecord(id: "call-unread-server", timestamp: now.addingTimeInterval(-10), readOnServer: false)
        ]

        let (model, effects) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in calls },
            clock: { now }
        )

        await model.loadCalls()
        XCTAssertTrue(model.readCallIds.contains("call-read-server"))
        XCTAssertFalse(model.isCallUnread(calls[0]))
        XCTAssertTrue(model.isCallUnread(calls[1]))
        XCTAssertEqual(effects.storedReadIds["contractor-1"], ["call-read-server"])
    }

    func testMarkAsReadRejectsForeignMembership() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        let calls = [makeRecord(id: "call-1", timestamp: now, transcript: "Kevin: Hi\nCaller: One\nCaller: Two")]
        let (model, effects) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in calls },
            clock: { now }
        )

        await model.loadCalls()
        XCTAssertTrue(model.isCallUnread(calls[0]))

        // Attempt to mark a foreign call ID not in owned snapshot
        let task = model.markAsRead(callId: "foreign-call-id")
        await task?.value

        XCTAssertFalse(model.readCallIds.contains("foreign-call-id"))
        XCTAssertTrue(effects.serverMarkedSids.isEmpty)
    }

    func testMarkAsReadRejectsExpectedAuthMismatch() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let wrongAuth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 99)
        let now = Date(timeIntervalSince1970: 1773532800)

        let calls = [makeRecord(id: "call-1", timestamp: now, transcript: "Kevin: Hi\nCaller: One\nCaller: Two")]
        let (model, effects) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in calls },
            clock: { now }
        )

        await model.loadCalls()

        let task = model.markAsRead(callId: "call-1", expectedAuth: wrongAuth)
        await task?.value

        XCTAssertFalse(model.readCallIds.contains("call-1"))
        XCTAssertTrue(effects.serverMarkedSids.isEmpty)
    }

    func testMarkRaceAfterAuthChangeWithoutExplicitInvalidate() async {
        var currentAuth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        let calls = [makeRecord(id: "call-1", timestamp: now, transcript: "Kevin: Hi\nCaller: One\nCaller: Two")]
        let (model, effects) = makeTestModel(
            authProvider: { currentAuth },
            fetchCalls: { _ in calls },
            clock: { now }
        )

        await model.loadCalls()

        // Auth rotates silently without explicit invalidate call
        currentAuth = CallAuthContext(contractorId: "contractor-2", bearerToken: "token-2", generation: 2)

        let task = model.markAsRead(callId: "call-1")
        await task?.value

        // Rejection: activeAuthContext does not match new currentAuth
        XCTAssertFalse(model.readCallIds.contains("call-1"))
        XCTAssertTrue(effects.serverMarkedSids.isEmpty)
    }

    func testMarkTaskQueuedAcrossAuthRotationDoesNotDispatch() async {
        var currentAuth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        let calls = [makeRecord(id: "call-1", timestamp: now, transcript: "Kevin: Hi\nCaller: One\nCaller: Two")]
        let (model, effects) = makeTestModel(
            authProvider: { currentAuth },
            fetchCalls: { _ in calls },
            clock: { now }
        )

        await model.loadCalls()

        // Rotate auth immediately before Task yields / executes
        let markTask = model.markAsRead(callId: "call-1")
        currentAuth = CallAuthContext(contractorId: "contractor-2", bearerToken: "token-2", generation: 2)

        await markTask?.value

        // Server mark was prevented because auth changed before async dispatch
        XCTAssertTrue(effects.serverMarkedSids.isEmpty)
    }

    func testMarkAllAsReadMarksEntireFetchedHistory() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        var calls: [CallRecord] = []
        for i in 0..<40 {
            calls.append(makeRecord(
                id: "call-\(i)",
                timestamp: now.addingTimeInterval(-Double(i * 10)),
                transcript: "Kevin: Hello\nCaller: Need quote\nCaller: Call back please"
            ))
        }

        let (model, effects) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in calls },
            clock: { now }
        )

        await model.loadCalls()
        XCTAssertEqual(model.visibleCalls.count, 20)
        XCTAssertEqual(model.unreadCount, 40)

        let markTask = model.markAllAsRead()
        await markTask?.value

        XCTAssertEqual(model.unreadCount, 0)
        XCTAssertEqual(model.readCallIds.count, 40)
        XCTAssertEqual(effects.serverMarkedSids.first?.sids.count, 40)
    }

    func testPaginationAndQueryPreservedOnRefresh() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        var calls: [CallRecord] = []
        for i in 0..<50 {
            calls.append(makeRecord(id: "call-\(i)", name: "Caller \(i)", timestamp: now.addingTimeInterval(-Double(i * 10))))
        }

        let (model, _) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in calls },
            clock: { now }
        )

        await model.loadCalls()
        model.showMore()
        model.setSearchQuery("Caller")
        XCTAssertEqual(model.visibleLimit, 20)
        model.showMore()
        XCTAssertEqual(model.visibleLimit, 40)

        // Refresh with same auth
        await model.loadCalls()

        XCTAssertEqual(model.visibleLimit, 40)
        XCTAssertEqual(model.searchQuery, "Caller")
        XCTAssertEqual(model.allCalls.count, 50)
    }

    // MARK: - 4. Search, Filter & Pagination Tests

    func testSearchSurfacesMatchesBeyondInitialPage() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        var calls: [CallRecord] = []
        for i in 0..<50 {
            let name = (i == 35) ? "Unique Matching Person" : "Caller \(i)"
            calls.append(makeRecord(id: "call-\(i)", name: name, timestamp: now.addingTimeInterval(-Double(i * 60))))
        }

        let (model, _) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in calls },
            clock: { now }
        )

        await model.loadCalls()
        XCTAssertEqual(model.allCalls.count, 50)
        XCTAssertEqual(model.visibleCalls.count, 20)

        // Query searches entire 50 calls before pagination
        model.setSearchQuery("Unique Matching")
        XCTAssertEqual(model.filteredCalls.count, 1)
        XCTAssertEqual(model.visibleCalls.count, 1)
        XCTAssertEqual(model.visibleCalls.first?.id, "call-35")
    }

    func testSearchCaseAndDiacriticInsensitive() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        let call = makeRecord(
            id: "call-accent",
            name: "René François",
            timestamp: now,
            transcript: "Kevin: Hello\nCaller: J'ai une fuite d'eau urgente dans la cuisine."
        )

        let (model, _) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in [call] },
            clock: { now }
        )

        await model.loadCalls()

        model.setSearchQuery("rene")
        XCTAssertEqual(model.filteredCalls.count, 1)

        model.setSearchQuery("FRANCOIS")
        XCTAssertEqual(model.filteredCalls.count, 1)

        model.setSearchQuery("fuite")
        XCTAssertEqual(model.filteredCalls.count, 1)
    }

    func testSearchPhoneDigitsNormalizationOnlyForPhoneLikeQuery() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        let phoneCall = makeRecord(id: "call-phone", phone: "+1 (555) 867-5309", timestamp: now)
        let otherCall = makeRecord(id: "call-other", name: "Bob", phone: "+1 (555) 123-4567", timestamp: now.addingTimeInterval(-10))

        let (model, _) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in [phoneCall, otherCall] },
            clock: { now }
        )

        await model.loadCalls()

        // Digits-only query matches normalized phone
        model.setSearchQuery("5558675309")
        XCTAssertEqual(model.filteredCalls.count, 1)
        XCTAssertEqual(model.filteredCalls.first?.id, "call-phone")

        // Phone punctuation formatted query matches
        model.setSearchQuery("(555) 867")
        XCTAssertEqual(model.filteredCalls.count, 1)
        XCTAssertEqual(model.filteredCalls.first?.id, "call-phone")

        // "invoice 2" contains letters, so it must NOT normalize digits and match phone with '2'
        model.setSearchQuery("invoice 2")
        XCTAssertEqual(model.filteredCalls.count, 0)
    }

    func testSearchDoesNotPerformHiddenTranscriptOrOutcomeWideSearch() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        let hiddenTranscriptCall = makeRecord(
            id: "call-hidden",
            name: "Normal Person",
            phone: "5551112222",
            timestamp: now,
            transcript: "Kevin: Secret word xyz in assistant text\nCaller: Just checking in"
        )

        let (model, _) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in [hiddenTranscriptCall] },
            clock: { now }
        )

        await model.loadCalls()

        // "Secret word" is in the assistant's line in transcript, not in callerExcerpt
        model.setSearchQuery("Secret word")
        XCTAssertEqual(model.filteredCalls.count, 0)
    }

    func testFilterUnreadAndSpam() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        let normalUnread = makeRecord(id: "call-unread", timestamp: now, transcript: "Kevin: Hi\nCaller: Line 1\nCaller: Line 2")
        let spamCall = makeRecord(id: "call-spam", timestamp: now.addingTimeInterval(-10), outcome: "spam")
        let blockedCall = makeRecord(id: "call-blocked", timestamp: now.addingTimeInterval(-20), outcome: "blocked")
        let readCall = makeRecord(id: "call-read", timestamp: now.addingTimeInterval(-30), readOnServer: true)

        let (model, _) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in [normalUnread, spamCall, blockedCall, readCall] },
            clock: { now }
        )

        await model.loadCalls()

        model.setFilter(.all)
        XCTAssertEqual(model.filteredCalls.count, 4)

        model.setFilter(.unread)
        XCTAssertEqual(model.filteredCalls.count, 1)
        XCTAssertEqual(model.filteredCalls.first?.id, "call-unread")

        model.setFilter(.spam)
        XCTAssertEqual(model.filteredCalls.count, 2)
        XCTAssertEqual(Set(model.filteredCalls.map { $0.id }), ["call-spam", "call-blocked"])
    }

    func testPaginationInitial20AndShowMore() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        var calls: [CallRecord] = []
        for i in 0..<45 {
            calls.append(makeRecord(id: "call-\(i)", timestamp: now.addingTimeInterval(-Double(i * 10))))
        }

        let (model, _) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in calls },
            clock: { now }
        )

        await model.loadCalls()
        XCTAssertEqual(model.visibleCalls.count, 20)
        XCTAssertTrue(model.canShowMore)
        XCTAssertEqual(model.showingCountLabel, "Showing 20 of 45 calls")

        model.showMore()
        XCTAssertEqual(model.visibleCalls.count, 40)
        XCTAssertTrue(model.canShowMore)
        XCTAssertEqual(model.showingCountLabel, "Showing 40 of 45 calls")

        model.showMore()
        XCTAssertEqual(model.visibleCalls.count, 45)
        XCTAssertFalse(model.canShowMore)
        XCTAssertEqual(model.showingCountLabel, "Showing 45 of 45 calls")
    }

    func testQueryOrFilterChangeResetsVisibleLimit() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        var calls: [CallRecord] = []
        for i in 0..<50 {
            calls.append(makeRecord(id: "call-\(i)", timestamp: now.addingTimeInterval(-Double(i * 10))))
        }

        let (model, _) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in calls },
            clock: { now }
        )

        await model.loadCalls()
        model.showMore()
        XCTAssertEqual(model.visibleLimit, 40)

        model.setSearchQuery("test")
        XCTAssertEqual(model.visibleLimit, 20)

        model.showMore()
        XCTAssertEqual(model.visibleLimit, 40)

        model.setFilter(.unread)
        XCTAssertEqual(model.visibleLimit, 20)
    }

    // MARK: - 5. Display Projections & Excerpts Tests

    func testDisplayNameAndFormattedPhone() {
        let namedCall = makeRecord(id: "1", name: "Jane Doe", phone: "5551234567", timestamp: Date())
        XCTAssertEqual(namedCall.displayName, "Jane Doe")
        XCTAssertEqual(namedCall.formattedPhone, "(555) 123-4567")

        let unnamedCall = makeRecord(id: "2", name: "", phone: "5551234567", timestamp: Date())
        XCTAssertEqual(unnamedCall.displayName, "(555) 123-4567")

        let emptyCall = makeRecord(id: "3", name: "  ", phone: "", timestamp: Date())
        XCTAssertEqual(emptyCall.displayName, "Unknown Caller")
    }

    func testCallerExcerptSelectionAndTruthfulOutcomes() {
        let callWithLongUtterance = makeRecord(
            id: "1",
            timestamp: Date(),
            transcript: "Kevin: Hi\nCaller: Quick hi\nCaller: My water heater is leaking all over the basement floor."
        )
        XCTAssertEqual(callWithLongUtterance.callerExcerpt, "My water heater is leaking all over the basement floor.")

        let callWithShortUtterance = makeRecord(
            id: "2",
            timestamp: Date(),
            transcript: "Kevin: Hi\nCaller: Quick hi"
        )
        XCTAssertEqual(callWithShortUtterance.callerExcerpt, "Quick hi")

        let voicemailCall = makeRecord(
            id: "3",
            timestamp: Date(),
            outcome: "voicemail",
            transcript: "Kevin: Please leave a message."
        )
        XCTAssertEqual(voicemailCall.callerExcerpt, "Left a voicemail.")

        let answeredCall = makeRecord(
            id: "4",
            timestamp: Date(),
            outcome: "picked_up",
            transcript: "Kevin: Hello."
        )
        XCTAssertEqual(answeredCall.callerExcerpt, "Answered.")

        let handledCall = makeRecord(
            id: "5",
            timestamp: Date(),
            outcome: "ignored",
            transcript: "Kevin: Hello."
        )
        XCTAssertEqual(handledCall.callerExcerpt, "Kevin handled the call.")

        let spamCall = makeRecord(
            id: "6",
            timestamp: Date(),
            outcome: "spam",
            transcript: "Caller: This is a scam call."
        )
        XCTAssertEqual(spamCall.callerExcerpt, "Marked as spam.")

        let blockedCall = makeRecord(
            id: "7",
            timestamp: Date(),
            outcome: "blocked",
            transcript: "Caller: Blocked robocall."
        )
        XCTAssertEqual(blockedCall.callerExcerpt, "Blocked call.")
    }

    // MARK: - 6. Local Mark Persistence Across Refreshes & Auth Guards

    func testRefreshPreservesSameSessionInMemoryReadMarks() async {
        let auth = CallAuthContext(contractorId: "contractor-1", bearerToken: "token-1", generation: 1)
        let now = Date(timeIntervalSince1970: 1773532800)

        let call1 = makeRecord(id: "call-1", timestamp: now)
        let call2 = makeRecord(id: "call-2", timestamp: now.addingTimeInterval(-10))

        let effects = MockHistoryEffects()
        let (model, _) = makeTestModel(
            authProvider: { auth },
            fetchCalls: { _ in [call1, call2] },
            effects: effects,
            clock: { now }
        )

        await model.loadCalls()
        XCTAssertEqual(model.unreadCount, 2)

        // Locally mark call-1 as read
        _ = model.markAsRead(callId: "call-1")
        XCTAssertEqual(model.unreadCount, 1)
        XCTAssertTrue(model.readCallIds.contains("call-1"))

        // Simulate a refresh where the external load effect returns empty stored IDs
        // and server has not yet stamped readOnServer: true
        effects.storedReadIds["contractor-1"] = []
        await model.loadCalls()

        // Local mark must NOT be regressed by the refresh
        XCTAssertTrue(model.readCallIds.contains("call-1"))
        XCTAssertEqual(model.unreadCount, 1)
    }
}
