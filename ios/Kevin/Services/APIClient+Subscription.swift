import Foundation

extension APIClient {
    // MARK: - Subscription

    func verifySubscription(
        transactionId: String,
        source: SubscriptionVerificationSource,
        context: SubscriptionVerificationContext
    ) async -> SubscriptionVerificationOutcome {
        guard !context.contractorID.isEmpty, !context.bearerToken.isEmpty else {
            return .rejected(reason: "missing_account_context")
        }
        do {
            let url = URL(string: "\(baseURL)/api/subscription/verify")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 15
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "transaction_id": transactionId,
                "contractor_id": context.contractorID,
                "source": source.rawValue,
                "app_build": Bundle.main.object(
                    forInfoDictionaryKey: "CFBundleVersion"
                ) as? String ?? "",
            ])
            request.setValue(
                "Bearer \(context.bearerToken)",
                forHTTPHeaderField: "Authorization"
            )
            // Verification has its own Retry-After contract. Do not route this
            // request through the generic one-second 5xx retry wrapper.
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                return .retryable(after: 30)
            }
            if http.statusCode == 401 {
                await MainActor.run {
                    SubscriptionVerificationResponseEffect.apply(
                        statusCode: http.statusCode,
                        capturedContext: context,
                        currentAuth: AppState.shared.currentAuthContext()
                    )
                }
            }
            return SubscriptionVerificationResponseParser.parse(data: data, response: http)
        } catch {
            debugLog("Verify subscription failed: \(error.localizedDescription)")
            return .retryable(after: 30)
        }
    }

    func signSubscriptionOffer(productId: String, offerId: String, applicationUsername: String) async -> [String: Any]? {
        let auth = await MainActor.run { AppState.shared.currentAuthContext() }
        return await signSubscriptionOffer(
            productId: productId,
            offerId: offerId,
            applicationUsername: applicationUsername,
            contractorId: auth.contractorId,
            bearerToken: auth.bearerToken
        )
    }

    func signSubscriptionOffer(
        productId: String,
        offerId: String,
        applicationUsername: String,
        contractorId: String,
        bearerToken: String
    ) async -> [String: Any]? {
        guard !contractorId.isEmpty, !bearerToken.isEmpty else { return nil }
        do {
            let url = URL(string: "\(baseURL)/api/subscription/sign-offer")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 15
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "contractor_id": contractorId,
                "product_id": productId,
                "offer_id": offerId,
                "application_username": applicationUsername,
            ])
            request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
            let (data, response) = try await retryRequest(request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let sig = json["signature"] as? [String: Any] {
                return sig
            }
        } catch {
            debugLog("Sign subscription offer failed: \(error.localizedDescription)")
        }
        return nil
    }

    func checkPromoEligibility(contractorId: String) async -> Bool {
        guard !contractorId.isEmpty else { return false }
        do {
            var components = URLComponents(string: "\(baseURL)/api/subscription/promo-eligible")!
            components.queryItems = [URLQueryItem(name: "contractor_id", value: contractorId)]
            var request = URLRequest(url: components.url!)
            request.timeoutInterval = 10
            authorize(&request)
            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                return json["eligible"] as? Bool ?? false
            }
        } catch {
            debugLog("Promo eligibility check failed: \(error.localizedDescription)")
        }
        return false
    }
}
