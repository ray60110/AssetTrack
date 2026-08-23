import LocalAuthentication
import Foundation

let account = CommandLine.arguments.dropFirst().first ?? ""
let reason = account.isEmpty
    ? "登入 AssetTrack 安全系統"
    : "解鎖 AssetTrack 帳號 \(account)"

let context = LAContext()
var error: NSError?

if context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error) {
    let semaphore = DispatchSemaphore(value: 0)
    var success = false

    context.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason) { (authSuccess, authError) in
        success = authSuccess
        semaphore.signal()
    }

    _ = semaphore.wait(timeout: .distantFuture)
    if success {
        exit(0)
    } else {
        exit(1)
    }
} else {
    exit(2)
}
