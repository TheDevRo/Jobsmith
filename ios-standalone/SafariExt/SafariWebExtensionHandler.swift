import SafariServices
import os.log
import JobsmithKit

/// Native side of the standalone Apply Assist extension. The JS layer
/// (common/api.js, swapped for api.native.js at build time) sends
/// {name, body} messages; everything — profile, job data, tailored DOCX,
/// answer bank, LLM field mapping — is served from the shared App Group
/// container via NativeMessageRouter.
final class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
    private static let log = OSLog(subsystem: "com.thedevro.jobsmith.standalone.Assist",
                                   category: "native-messaging")

    func beginRequest(with context: NSExtensionContext) {
        let raw = (context.inputItems.first as? NSExtensionItem)?
            .userInfo?[SFExtensionMessageKey]
        guard let message = raw as? [String: Any],
              let name = message["name"] as? String else {
            respond(context, ["error": "Malformed native message", "status": 400])
            return
        }
        let body = message["body"] as? [String: Any] ?? [:]

        Task {
            let reply: [String: Any]
            do {
                let db = try AppDatabase.shared()
                let router = NativeMessageRouter(
                    db: db,
                    engine: OpenAICompatibleEngine(),
                    config: { await ConfigStore.shared.reload() }
                )
                reply = await router.handle(name: name, body: body)
            } catch {
                os_log(.error, log: Self.log, "database unavailable: %{public}@",
                       error.localizedDescription)
                reply = ["error": "Jobsmith app data unavailable — open the app once first.",
                         "status": 500]
            }
            respond(context, reply)
        }
    }

    private func respond(_ context: NSExtensionContext, _ payload: [String: Any]) {
        let response = NSExtensionItem()
        response.userInfo = [SFExtensionMessageKey: payload]
        context.completeRequest(returningItems: [response])
    }
}
