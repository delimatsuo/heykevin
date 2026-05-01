import Foundation
import AuthenticationServices
import UIKit

/// Result of a fresh Apple identity token request.
struct AppleIdentityRefreshResult {
    let appleUserId: String
    let identityToken: String
}

enum AppleIdentityRefreshError: Error, LocalizedError {
    /// User cancelled the system Sign-in with Apple sheet.
    case cancelled
    /// Apple returned no identity token in the credential.
    case missingIdentityToken
    /// Underlying ASAuthorizationError or other failure.
    case underlying(Error)

    var errorDescription: String? {
        switch self {
        case .cancelled:
            return String(localized: "Sign in was cancelled.")
        case .missingIdentityToken:
            return String(localized: "Apple did not return an identity token. Please try again.")
        case .underlying(let error):
            return error.localizedDescription
        }
    }
}

/// Wraps `ASAuthorizationController` so that a fresh Apple identity token can be
/// requested programmatically (without the user tapping `SignInWithAppleButton`)
/// when the previously-captured token has expired (~10 min).
///
/// Strategy:
///  1. Try `.operationRefresh` for the existing `appleUserId` — silent on
///     devices where the user is still signed in to the same Apple ID and the
///     prior credential is still cached by the system.
///  2. If refresh fails (no credential available, user signed out, etc.), fall
///     back to a normal `.operationLogin` re-prompt — the system will show the
///     standard Sign-in with Apple sheet.
final class AppleIdentityRefresher: NSObject, @unchecked Sendable {
    static let shared = AppleIdentityRefresher()

    // Mutated only on the main thread (request kick-off) and from
    // ASAuthorizationController delegate callbacks (which Apple invokes on
    // the main thread). No additional synchronisation is required.
    private var continuation: CheckedContinuation<AppleIdentityRefreshResult, Error>?
    private var activeController: ASAuthorizationController?

    private override init() {
        super.init()
    }

    /// Request a fresh identity token. Tries silent refresh first; falls back
    /// to an interactive Sign-in with Apple prompt if refresh is not possible.
    @MainActor
    func refreshIdentityToken(existingUserId: String) async throws -> AppleIdentityRefreshResult {
        do {
            return try await performRequest(operation: .operationRefresh, user: existingUserId)
        } catch AppleIdentityRefreshError.cancelled {
            // User explicitly cancelled — propagate, don't loop into a re-prompt.
            throw AppleIdentityRefreshError.cancelled
        } catch {
            // Silent refresh failed for any other reason — fall back to a full
            // re-prompt. Apple only honours `.operationRefresh` in narrow
            // circumstances, so the login fallback is the common path.
            return try await performRequest(operation: .operationLogin, user: nil)
        }
    }

    @MainActor
    private func performRequest(
        operation: ASAuthorization.OpenIDOperation,
        user: String?
    ) async throws -> AppleIdentityRefreshResult {
        // Serialize requests — only one outstanding ASAuthorization at a time.
        if continuation != nil {
            throw AppleIdentityRefreshError.underlying(
                NSError(
                    domain: "AppleIdentityRefresher",
                    code: -1,
                    userInfo: [NSLocalizedDescriptionKey: "Another sign-in request is already in progress."]
                )
            )
        }

        return try await withCheckedThrowingContinuation { (cont: CheckedContinuation<AppleIdentityRefreshResult, Error>) in
            self.continuation = cont

            let provider = ASAuthorizationAppleIDProvider()
            let request = provider.createRequest()
            request.requestedOperation = operation
            if operation == .operationLogin {
                request.requestedScopes = [.fullName, .email]
            }
            if let user = user, !user.isEmpty {
                request.user = user
            }

            let controller = ASAuthorizationController(authorizationRequests: [request])
            controller.delegate = self
            controller.presentationContextProvider = self
            self.activeController = controller
            controller.performRequests()
        }
    }

    private func finish(with result: Result<AppleIdentityRefreshResult, Error>) {
        let cont = continuation
        continuation = nil
        activeController = nil
        switch result {
        case .success(let value):
            cont?.resume(returning: value)
        case .failure(let error):
            cont?.resume(throwing: error)
        }
    }
}

extension AppleIdentityRefresher: ASAuthorizationControllerDelegate {
    func authorizationController(
        controller: ASAuthorizationController,
        didCompleteWithAuthorization authorization: ASAuthorization
    ) {
        guard let credential = authorization.credential as? ASAuthorizationAppleIDCredential else {
            finish(with: .failure(AppleIdentityRefreshError.missingIdentityToken))
            return
        }
        guard
            let tokenData = credential.identityToken,
            let token = String(data: tokenData, encoding: .utf8),
            !token.isEmpty
        else {
            finish(with: .failure(AppleIdentityRefreshError.missingIdentityToken))
            return
        }
        let result = AppleIdentityRefreshResult(
            appleUserId: credential.user,
            identityToken: token
        )
        finish(with: .success(result))
    }

    func authorizationController(
        controller: ASAuthorizationController,
        didCompleteWithError error: Error
    ) {
        if let asError = error as? ASAuthorizationError, asError.code == .canceled {
            finish(with: .failure(AppleIdentityRefreshError.cancelled))
        } else {
            finish(with: .failure(AppleIdentityRefreshError.underlying(error)))
        }
    }
}

extension AppleIdentityRefresher: ASAuthorizationControllerPresentationContextProviding {
    func presentationAnchor(for controller: ASAuthorizationController) -> ASPresentationAnchor {
        // Apple calls this synchronously on the main thread.
        let scenes = UIApplication.shared.connectedScenes
        for scene in scenes {
            if let windowScene = scene as? UIWindowScene,
               let window = windowScene.windows.first(where: { $0.isKeyWindow }) ?? windowScene.windows.first {
                return window
            }
        }
        return ASPresentationAnchor()
    }
}
