import XCTest
@testable import Kevin

@MainActor
final class TranscriptPresentationTests: XCTestCase {
    func testScreeningReasonParserUsesOnlyBoundedStructuredField() throws {
        for (value, expected): (Any?, String) in [("  Water leak\n", "Water leak"), (nil, ""), (123, ""), ("Speaking with Kevin", ""), (String(repeating: "Z", count: 200), String(repeating: "Z", count: 160))] {
            var json: [String: Any] = ["status": "ok", "active": true, "call_sid": "CA_1", "contractor_id": "owner", "action_status": "ready", "transcript": "Caller: unrelated text"]
            json["screening_reason"] = value
            let result = CallActionResponseParser.parse(data: try JSONSerialization.data(withJSONObject: json),
                response: HTTPURLResponse(url: URL(string: "https://fixture.invalid")!, statusCode: 200, httpVersion: nil, headerFields: nil)!,
                requestedCallSid: "CA_1", requestedContractorId: "owner", requestedOperationId: "", requestedAction: "", isGet: true)
            XCTAssertTrue(result.validNavigation(callSid: "CA_1", contractorId: "owner"))
            XCTAssertEqual(result.screeningReason, expected)
        }
    }

    func testSnapshotIsFrozenAndInvalidAcrossAuthRoundTripAndCallReplacement() throws {
        let firstAuth = CallAuthContext(contractorId: "owner", bearerToken: "fixture-token", generation: 1)
        var auth = firstAuth
        let state = AppState(authProvider: { auth }, inMemory: true)
        state.setActiveCall(callSid: "CA_1", callerPhone: "", callerName: "", authContext: auth)
        state.updateActiveCallTranscript(text: "Caller: Before pickup", authContext: auth, callSid: "CA_1")
        let first = try XCTUnwrap(state.ownedActiveCallLease)
        state.captureScreeningTranscript(for: first)
        state.updateActiveCallTranscript(text: "Caller: Later update", authContext: auth, callSid: "CA_1")
        state.captureScreeningTranscript(for: first)
        XCTAssertEqual(state.screeningTranscript(for: first)?.lines, ["Caller: Before pickup"])
        auth = CallAuthContext(contractorId: "owner", bearerToken: "fixture-token", generation: 3)
        XCTAssertNil(state.screeningTranscript(for: first), "Same account after a session round trip cannot revive a snapshot")
        state.applyAuthChange()
        state.setActiveCall(callSid: "CA_2", callerPhone: "", callerName: "", authContext: auth)
        let second = try XCTUnwrap(state.ownedActiveCallLease)
        state.captureScreeningTranscript(for: second)
        state.clearScreeningTranscript(for: first)
        XCTAssertNotNil(state.screeningTranscript(for: second), "Old cleanup cannot erase a new call's snapshot")
        XCTAssertEqual(state.screeningTranscript(for: second)?.lines, [])
        state.clearActiveCall()
        XCTAssertNil(state.screeningTranscript(for: second))
    }

    func testStaleLeaseCannotWriteReasonToRecreatedSameCall() throws {
        let auth = CallAuthContext(contractorId: "owner", bearerToken: "fixture-token", generation: 1)
        let state = AppState(authProvider: { auth }, inMemory: true)
        state.setActiveCall(callSid: "CA_1", callerPhone: "", callerName: "", authContext: auth)
        let old = try XCTUnwrap(state.ownedActiveCallLease)
        state.clearActiveCall()
        state.setActiveCall(callSid: "CA_1", callerPhone: "", callerName: "", authContext: auth)
        state.updateActiveCallReason(reason: "Stale reason", lease: old)
        XCTAssertEqual(state.activeCallReason, "")
        state.captureScreeningTranscript(for: old)
        XCTAssertNil(state.screeningTranscript(for: old))
    }
}
