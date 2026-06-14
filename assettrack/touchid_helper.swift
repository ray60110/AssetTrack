import LocalAuthentication
import Foundation

let context = LAContext()
var error: NSError?

// Check if biometric authentication is available
if context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error) {
    let reason = "登入 AssetTrack 安全系統"
    let semaphore = DispatchSemaphore(value: 0)
    var success = false
    
    context.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason) { (authSuccess, authError) in
        success = authSuccess
        semaphore.signal()
    }
    
    _ = semaphore.wait(timeout: .distantFuture)
    if success {
        exit(0) // Auth Success
    } else {
        exit(1) // Auth Failed (Cancel or Fail)
    }
} else {
    // Biometric not available (no TouchID configured or not supported)
    exit(2)
}
