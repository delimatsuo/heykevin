import Foundation
import CoreFoundation

struct ActiveCallInfo {
    let callSid: String
    let callerPhone: String
    let callerName: String
    let transcript: String
}

struct CallActionResult: Equatable, Sendable {
    let callSid: String
    let contractorId: String
    let operationId: String
    let action: String
    let actionStatus: String
    let accessToken: String?
    let conferenceName: String?
    let isActive: Bool
    let isUrgent: Bool
    let callerName: String?
    let callerPhone: String?
    let transcript: String?
    let statusCode: Int
    let rawStatus: String?
    let errorDetail: String?
    var retryable: Bool = false
    var hasExplicitActive: Bool = true
    var screeningReason: String = ""

    var isPreparationFailure: Bool {
        statusCode == 503 && rawStatus == "error" && action == "accept" && actionStatus == "preparation_failed"
        && retryable && hasExplicitActive && isActive && accessToken == nil && conferenceName == nil
    }
    var isAccepted: Bool {
        statusCode == 200 && rawStatus == "ok" && hasExplicitActive && isActive && action == "accept" && actionStatus == "accepted"
        && !(accessToken?.isEmpty ?? true) && !(conferenceName?.isEmpty ?? true)
    }
    var isTakingMessage: Bool {
        statusCode == 200 && rawStatus == "ok" && hasExplicitActive && isActive && action == "decline" && actionStatus == "taking_message"
        && accessToken == nil && conferenceName == nil
    }
    var isPending: Bool {
        statusCode == 202 && rawStatus == "pending" && hasExplicitActive && isActive && accessToken == nil && conferenceName == nil
        && ((action == "accept" && ["accepting", "uncertain"].contains(actionStatus))
            || (action == "decline" && ["message_requested", "uncertain"].contains(actionStatus)))
    }
    var isEnded: Bool { statusCode == 200 && rawStatus == "ok" && hasExplicitActive && !isActive && actionStatus == "ended" }
    var isConflict: Bool { statusCode == 409 && rawStatus == "error" && actionStatus == "action_conflict" && accessToken == nil && conferenceName == nil }
    func matchesAction(callSid: String, contractorId: String, operationId: String, action: String) -> Bool {
        self.callSid == callSid && self.contractorId == contractorId && self.operationId == operationId
        && (isEnded || isConflict || (self.action == action && (isAccepted || isTakingMessage || isPending || isPreparationFailure)))
    }
    func validNavigation(callSid: String, contractorId: String) -> Bool {
        guard self.callSid == callSid, self.contractorId == contractorId,
              accessToken == nil, conferenceName == nil else { return false }
        if isEnded { return true }
        if statusCode == 200 && rawStatus == "ok" && hasExplicitActive && isActive {
            return ["ready", "accepted", "taking_message", "action_conflict"].contains(actionStatus)
        }
        return statusCode == 202 && rawStatus == "pending" && hasExplicitActive && isActive
            && ["accepting", "message_requested", "uncertain"].contains(actionStatus)
    }
}

enum CallActionResponseParser {
    static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }
    static func parse(
        data: Data,
        response: HTTPURLResponse,
        requestedCallSid: String,
        requestedContractorId: String,
        requestedOperationId: String,
        requestedAction: String,
        isGet: Bool = false
    ) -> CallActionResult {
        let statusCode = response.statusCode
        let json = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]

        let rawStatus = json["status"] as? String
        let callSid = json["call_sid"] as? String ?? ""
        let contractorId = json["contractor_id"] as? String ?? ""
        let operationId = json["operation_id"] as? String ?? ""
        let action = json["action"] as? String ?? ""
        let actionStatus = json["action_status"] as? String ?? ""
        let accessToken = json["access_token"] as? String
        let conferenceName = json["conference_name"] as? String
        let active = boolean(json["active"]) ?? false
        let urgent = boolean(json["urgent"]) ?? false
        let callerName = json["caller_name"] as? String
        let callerPhone = json["caller_phone"] as? String
        let transcript = json["transcript"] as? String
        let errorDetail = json["error"] as? String ?? json["detail"] as? String ?? json["message"] as? String
        var screeningReason = ""
        if let rawReason = json["screening_reason"] as? String {
            let trimmed = rawReason.trimmingCharacters(in: .whitespacesAndNewlines)
            screeningReason = trimmed == "Speaking with Kevin" ? "" : String(trimmed.prefix(160))
        }

        // Only the genuine legacy POST accept shape is synthesized. A partial v2
        // response or any decline acknowledgement must never become success.
        let identityKeys = ["call_sid", "contractor_id", "operation_id", "action", "action_status", "active"]
        if !isGet && statusCode == 200 && rawStatus == "ok" && requestedAction == "accept",
           identityKeys.allSatisfy({ json[$0] == nil }), Set(json.keys).isSubset(of: ["status", "access_token", "conference_name"]),
           let token = accessToken, !token.isEmpty, let conference = conferenceName, !conference.isEmpty {
            return CallActionResult(callSid: requestedCallSid, contractorId: requestedContractorId,
                operationId: requestedOperationId, action: "accept", actionStatus: "accepted",
                accessToken: token, conferenceName: conference, isActive: true, isUrgent: false,
                callerName: nil, callerPhone: nil, transcript: nil, statusCode: 200, rawStatus: "ok", errorDetail: nil)
        }

        return CallActionResult(
            callSid: callSid,
            contractorId: contractorId,
            operationId: operationId,
            action: action,
            actionStatus: actionStatus,
            accessToken: accessToken,
            conferenceName: conferenceName,
            isActive: active,
            isUrgent: urgent,
            callerName: callerName,
            callerPhone: callerPhone,
            transcript: transcript,
            statusCode: statusCode,
            rawStatus: rawStatus,
            errorDetail: errorDetail,
            retryable: boolean(json["retryable"]) ?? false,
            hasExplicitActive: boolean(json["active"]) != nil,
            screeningReason: screeningReason
        )
    }
}

/// A failed appointment confirmation, carrying the status so the UI can tell a
/// permanent problem from a retryable one.
///
/// A live call on 2026-08-19 stored "Friday, August 21st" as the year 2020 —
/// the model had no current date to resolve against. `slot_is_plausible`
/// rejected it with 422, but the app rendered every failure as "Couldn't add
/// this to Google Calendar. Try again.", inviting a retry that could never
/// succeed. Same shape as the bug PR #156 fixed on the voice side, where a
/// permanent denial read as a transient fault and got retried four times.
struct AppointmentConfirmFailure: Error, LocalizedError {
    let statusCode: Int

    /// 422: the stored time itself is unusable. Retrying cannot help — the
    /// owner needs to reach the caller and agree a real time.
    var isTimeUnusable: Bool { statusCode == 422 }

    var errorDescription: String? {
        isTimeUnusable
            ? String(localized: "Kevin didn't capture a usable time for this request. Use Call Back to agree a time with the caller.")
            : String(localized: "Couldn't add this to Google Calendar. Try again.")
    }
}

enum CallHistoryError: Error, LocalizedError, Equatable {
    case unauthorized
    case forbidden
    case serverError(statusCode: Int)
    case httpError(statusCode: Int)
    case malformedResponse
    case invalidAuth

    var errorDescription: String? {
        switch self {
        case .unauthorized:
            return String(localized: "Session expired. Please sign in again.")
        case .forbidden:
            return String(localized: "You do not have access to call history.")
        case .serverError(let code):
            return String(localized: "Server error (\(code)). Please try again later.")
        case .httpError(let code):
            return String(localized: "Request failed with status \(code).")
        case .malformedResponse:
            return String(localized: "Received an invalid response from the server.")
        case .invalidAuth:
            return String(localized: "Authentication required.")
        }
    }
}

enum CallHistoryResponseParser {
    static func parse(data: Data, response: HTTPURLResponse) throws -> [CallRecord] {
        let statusCode = response.statusCode
        if statusCode == 401 {
            throw CallHistoryError.unauthorized
        }
        if statusCode == 403 {
            throw CallHistoryError.forbidden
        }
        if (500...599).contains(statusCode) {
            throw CallHistoryError.serverError(statusCode: statusCode)
        }
        guard (200...299).contains(statusCode) else {
            throw CallHistoryError.httpError(statusCode: statusCode)
        }

        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let callsArray = json["calls"] as? [[String: Any]] else {
            throw CallHistoryError.malformedResponse
        }

        return try callsArray.map { dict -> CallRecord in
            guard let rawSid = dict["call_sid"] as? String else {
                throw CallHistoryError.malformedResponse
            }
            let callSid = rawSid.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !callSid.isEmpty else {
                throw CallHistoryError.malformedResponse
            }

            guard let rawTimestampNum = dict["timestamp"] as? NSNumber,
                  CFGetTypeID(rawTimestampNum) != CFBooleanGetTypeID() else {
                throw CallHistoryError.malformedResponse
            }
            let rawTimestamp = rawTimestampNum.doubleValue
            guard rawTimestamp.isFinite, !rawTimestamp.isNaN, rawTimestamp > 0 else {
                throw CallHistoryError.malformedResponse
            }

            let timestamp = Date(timeIntervalSince1970: rawTimestamp)
            let serverRead = dict["read"] as? Bool ?? false
            let appointment = dict["appointment_request"] as? [String: Any]

            return CallRecord(
                id: callSid,
                callerPhone: dict["caller_phone"] as? String ?? "",
                callerName: dict["caller_name"] as? String ?? "",
                timestamp: timestamp,
                trustScore: dict["trust_score"] as? Int ?? 0,
                outcome: dict["outcome"] as? String ?? "unknown",
                transcript: dict["transcript"] as? String ?? "",
                voicemailURL: dict["voicemail_url"] as? String,
                callbackNumber: dict["callback_number"] as? String,
                readOnServer: serverRead,
                appointmentStatus: appointment?["status"] as? String,
                appointmentStartTime: appointment?["start_time"] as? String,
                appointmentTitle: appointment?["title"] as? String,
                appointmentCallerNotified: appointment?["caller_notified_at"] != nil,
                tags: (dict["tags"] as? [Any] ?? []).compactMap { ($0 as? String).flatMap(CallTag.init(rawValue:)) }
            )
        }
    }
}

extension APIClient {
    // MARK: - Active Call Check

    func getActiveCall(authContext: CallAuthContext? = nil) async -> ActiveCallInfo? {
        let current = await MainActor.run { AppState.shared.currentAuthContext() }
        let auth = authContext ?? current
        let contractorId = auth.contractorId
        guard auth.isValid else { return nil }

        do {
            var components = URLComponents(string: "\(baseURL)/api/active-call")!
            components.queryItems = [URLQueryItem(name: "contractor_id", value: contractorId)]
            let url = components.url!
            var request = URLRequest(url: url)
            request.timeoutInterval = 5
            request.setValue("Bearer \(auth.bearerToken)", forHTTPHeaderField: "Authorization")

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               json["active"] as? Bool == true {
                return ActiveCallInfo(
                    callSid: json["call_sid"] as? String ?? "",
                    callerPhone: json["caller_phone"] as? String ?? "",
                    callerName: json["caller_name"] as? String ?? "",
                    transcript: json["transcript"] as? String ?? ""
                )
            }
        } catch {
            debugLog("Active call check failed: \(error.localizedDescription)")
        }
        return nil
    }

    // MARK: - Call Actions

    private func applyCallActionUnauthorizedFence(
        statusCode: Int,
        contractorId: String,
        bearerToken: String,
        generation: Int
    ) async {
        guard statusCode == 401 else { return }
        await MainActor.run {
            if AppState.shared.currentAuthContext().matches(contractorId: contractorId, bearerToken: bearerToken, generation: generation) {
                AppState.shared.needsReauth = true
            }
        }
    }

    func sendCallAction(
        callSid: String,
        action: String,
        operationId: String = "",
        message: String = "",
        contractorId: String? = nil,
        bearerToken: String? = nil,
        sessionGeneration: Int? = nil
    ) async throws -> CallActionResult {
        let captured = await MainActor.run { AppState.shared.currentAuthContext() }
        let requestGeneration = sessionGeneration ?? captured.generation
        let activeContractorId = contractorId ?? captured.contractorId
        let activeToken = bearerToken ?? captured.bearerToken
        guard !activeContractorId.isEmpty, !activeToken.isEmpty else {
            throw URLError(.userAuthenticationRequired)
        }

        var components = URLComponents(string: "\(baseURL)/api/call-action")!
        components.queryItems = [URLQueryItem(name: "contractor_id", value: activeContractorId)]
        let url = components.url!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        var body: [String: Any] = [
            "call_sid": callSid,
            "action": action,
            "contractor_id": activeContractorId,
        ]
        if !operationId.isEmpty {
            body["operation_id"] = String(operationId.prefix(80))
        }
        if !message.isEmpty {
            body["message"] = message
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        request.setValue("Bearer \(activeToken)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }

        await applyCallActionUnauthorizedFence(
            statusCode: http.statusCode,
            contractorId: activeContractorId,
            bearerToken: activeToken,
            generation: requestGeneration
        )

        return CallActionResponseParser.parse(
            data: data,
            response: http,
            requestedCallSid: callSid,
            requestedContractorId: activeContractorId,
            requestedOperationId: operationId,
            requestedAction: action,
            isGet: false
        )
    }

    func getCallAction(
        callSid: String,
        operationId: String = "",
        contractorId: String? = nil,
        bearerToken: String? = nil,
        sessionGeneration: Int? = nil
    ) async throws -> CallActionResult? {
        let captured = await MainActor.run { AppState.shared.currentAuthContext() }
        let requestGeneration = sessionGeneration ?? captured.generation
        let activeContractorId = contractorId ?? captured.contractorId
        let activeToken = bearerToken ?? captured.bearerToken
        guard !activeContractorId.isEmpty, !activeToken.isEmpty, !callSid.isEmpty else { return nil }

        let encodedSid = callSid.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? callSid
        var components = URLComponents(string: "\(baseURL)/api/call-action/\(encodedSid)")!
        var queryItems = [URLQueryItem(name: "contractor_id", value: activeContractorId)]
        if !operationId.isEmpty {
            queryItems.append(URLQueryItem(name: "operation_id", value: String(operationId.prefix(80))))
        }
        components.queryItems = queryItems
        guard let url = components.url else { return nil }

        var request = URLRequest(url: url)
        request.timeoutInterval = 5
        request.setValue("Bearer \(activeToken)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { return nil }

        await applyCallActionUnauthorizedFence(
            statusCode: http.statusCode,
            contractorId: activeContractorId,
            bearerToken: activeToken,
            generation: requestGeneration
        )

        return CallActionResponseParser.parse(
            data: data,
            response: http,
            requestedCallSid: callSid,
            requestedContractorId: activeContractorId,
            requestedOperationId: operationId,
            requestedAction: "",
            isGet: true
        )
    }

    func sendCallAction(callSid: String, action: String, message: String = "") async -> [String: Any]? {
        do {
            let result = try await sendCallAction(
                callSid: callSid,
                action: action,
                operationId: "",
                message: message,
                contractorId: nil,
                bearerToken: nil
            )
            var dict: [String: Any] = [
                "status": result.rawStatus ?? (result.statusCode == 200 ? "ok" : "error"),
                "call_sid": result.callSid,
                "action": result.action,
                "action_status": result.actionStatus,
            ]
            if let token = result.accessToken { dict["access_token"] = token }
            if let conf = result.conferenceName { dict["conference_name"] = conf }
            return dict
        } catch {
            debugLog("Call action failed: \(error.localizedDescription)")
            return nil
        }
    }

    // MARK: - Call History

    func getCallHistory(authContext: CallAuthContext? = nil) async throws -> [CallRecord] {
        let current = await MainActor.run { AppState.shared.currentAuthContext() }
        let auth = authContext ?? current
        let contractorId = auth.contractorId
        guard auth.isValid, !contractorId.isEmpty else {
            debugLog("Call history: invalid auth or no contractor ID")
            throw CallHistoryError.invalidAuth
        }
        var components = URLComponents(string: "\(baseURL)/api/calls")!
        components.queryItems = [URLQueryItem(name: "contractor_id", value: contractorId)]
        guard let url = components.url else {
            throw CallHistoryError.malformedResponse
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 10
        request.setValue("Bearer \(auth.bearerToken)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await retryRequest(request, signalReauth: false)
        guard let http = response as? HTTPURLResponse else {
            throw CallHistoryError.malformedResponse
        }
        return try CallHistoryResponseParser.parse(data: data, response: http)
    }

    func markCallsRead(_ callSids: [String], authContext: CallAuthContext? = nil) async {
        guard !callSids.isEmpty else { return }
        let current = await MainActor.run { AppState.shared.currentAuthContext() }
        let auth = authContext ?? current
        guard auth.isValid else { return }

        guard let url = URL(string: "\(baseURL)/api/calls/mark-read") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.setValue("Bearer \(auth.bearerToken)", forHTTPHeaderField: "Authorization")
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "call_sids": callSids,
            "contractor_id": auth.contractorId
        ])
        _ = try? await retryRequest(request, signalReauth: false)
    }

    /// Returns whether the backend actually texted the caller.
    ///
    /// The UI must not claim a text was sent when it wasn't: the send is
    /// skipped when the caller is the owner's own number (self-test) and can
    /// fail outright when the caller has replied STOP. `caller_notified` is
    /// the server's answer to that, so the confirmation card reads it instead
    /// of assuming.
    @discardableResult
    func confirmAppointment(callSid: String, authContext: CallAuthContext? = nil) async throws -> Bool {
        let current = await MainActor.run { AppState.shared.currentAuthContext() }
        let auth = authContext ?? current
        guard auth.isValid, !auth.contractorId.isEmpty else {
            throw URLError(.userAuthenticationRequired)
        }
        let encoded = callSid.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? callSid
        guard var components = URLComponents(string: "\(baseURL)/api/calls/\(encoded)/confirm-appointment") else {
            throw URLError(.badURL)
        }
        components.queryItems = [URLQueryItem(name: "contractor_id", value: auth.contractorId)]
        guard let url = components.url else {
            throw URLError(.badURL)
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 15
        request.setValue("Bearer \(auth.bearerToken)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await retryRequest(request, signalReauth: false)
        guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            throw AppointmentConfirmFailure(statusCode: status)
        }

        let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        return (json?["caller_notified"] as? Bool) ?? false
    }
}
