import Foundation

enum AccountDeletionOutcome: Equatable {
    case deleted
    case failed
}

/// Decides whether the server confirmed an account deletion. Local state
/// (contractor ID, token, onboarding) may only be cleared on `.deleted`;
/// clearing it on failure strands the user — logged out locally while the
/// account stays active and billing, with no credentials left to retry.
enum AccountDeletionResponseParser {
    static func parse(response: URLResponse?, data: Data? = nil) -> AccountDeletionOutcome {
        guard let http = response as? HTTPURLResponse else { return .failed }
        // Already gone: after a deletion commits server-side the token stops
        // authenticating (the lookup filters active==True), so a 401 on a
        // deletion attempt is the only signal the app will ever get that the
        // account is deleted — treating it as failure would strand the user
        // in a permanent retry loop. 404 means the document no longer exists.
        if http.statusCode == 401 || http.statusCode == 404 {
            return .deleted
        }
        guard (200...299).contains(http.statusCode) else {
            return .failed
        }
        // Fail-closed on 2xx: only the backend's explicit {"status": "ok"}
        // confirms deletion. An empty, HTML, or otherwise unparseable body
        // may be a middlebox or LB page that never reached kevin-api —
        // wiping credentials on it would strand a still-billing account.
        // (Pre-5xx-fix backends report failure as 200 {"status": "error"},
        // which this check also rejects.)
        guard let data,
              let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              json["status"] as? String == "ok" else {
            return .failed
        }
        return .deleted
    }
}

enum AccountDeletionFirstStep: Equatable {
    case warnActiveSubscription
    case confirmDelete
}

/// First step of the delete-account flow. Deletion is a server-side
/// deactivation and does NOT cancel the user's Apple subscription (which may
/// continue auto-renewing if not turned off), so a user with an active or
/// purchased subscription is directed to check Manage Subscription before
/// account deletion.
enum AccountDeletionFlow {
    static let warningTitle: String.LocalizationValue = "Check Apple Subscription"
    static let warningBody: String.LocalizationValue = "Deleting your Hey Kevin account does not cancel your Apple subscription. Check Manage Subscription first to make sure automatic renewal is off."

    /// Server truth first, Keychain cache as fallback (field-wise): the
    /// cache defaults to "trial" and can be stale, and a stale skip of the
    /// billing warning means an active subscriber deletes without it.
    static func resolve(
        freshStatus: String?, freshTier: String?,
        cachedStatus: String, cachedTier: String
    ) -> (status: String, tier: String) {
        (freshStatus ?? cachedStatus, freshTier ?? cachedTier)
    }

    static func firstStep(subscriptionStatus: String, subscriptionTier: String) -> AccountDeletionFirstStep {
        switch subscriptionStatus {
        case "active":
            // "active" implies a subscription regardless of the cached tier.
            return .warnActiveSubscription
        case "expired", "cancelled":
            // "expired" includes Apple's billing-retry window and "cancelled"
            // subscriptions run to period end — both can still charge a
            // deleted account, but only for someone who actually purchased.
            // The trial sweep marks never-subscribed accounts "expired" too;
            // their tier stays "none", and warning them about charges that
            // cannot exist would be false.
            let purchased = !subscriptionTier.isEmpty && subscriptionTier != "none"
            return purchased ? .warnActiveSubscription : .confirmDelete
        default:
            return .confirmDelete
        }
    }
}
