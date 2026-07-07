import SafariServices
import os.log

/// Native side of the Jobsmith Assist Safari Web Extension. The extension's
/// JavaScript talks straight to the Jobsmith backend over HTTP (configured in
/// the extension popup), so no native messaging is needed — this handler just
/// satisfies the extension-point contract.
final class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
    func beginRequest(with context: NSExtensionContext) {
        let message = (context.inputItems.first as? NSExtensionItem)?
            .userInfo?[SFExtensionMessageKey]
        os_log(.default, "Jobsmith Assist received native message: %@",
               String(describing: message))

        let response = NSExtensionItem()
        response.userInfo = [SFExtensionMessageKey: ["ok": true]]
        context.completeRequest(returningItems: [response])
    }
}
