import Foundation

struct PhoneFormatter {
    static func format(_ phone: String) -> String {
        let digits = phone.filter { $0.isNumber }
        if digits.count == 11, digits.hasPrefix("1") {
            let area = digits.dropFirst().prefix(3)
            let mid = digits.dropFirst(4).prefix(3)
            let last = digits.suffix(4)
            return "(\(area)) \(mid)-\(last)"
        }
        if digits.count == 10 {
            let area = digits.prefix(3)
            let mid = digits.dropFirst(3).prefix(3)
            let last = digits.suffix(4)
            return "(\(area)) \(mid)-\(last)"
        }
        return phone
    }
}
