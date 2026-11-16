import SwiftUI

struct ChannelBugView: View {
    let number: String
    let name: String
    let title: String

    private let phosphor = Color(red: 70/255, green: 1.0, blue: 122/255)

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text("CH \(number)")
                    .font(.system(size: 22, weight: .bold, design: .monospaced))
                    .foregroundColor(phosphor)
                Text(name.uppercased())
                    .font(.system(size: 16, weight: .semibold, design: .monospaced))
                    .foregroundColor(phosphor)
                    .lineLimit(1)
            }
            if !title.isEmpty {
                Text(title)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundColor(Color(red: 228/255, green: 247/255, blue: 233/255))
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(Color(red: 4/255, green: 16/255, blue: 8/255, opacity: 0.86))
        .overlay(
            Rectangle()
                .frame(height: 1)
                .foregroundColor(phosphor.opacity(0.45)),
            alignment: .top
        )
        .overlay(
            Rectangle()
                .frame(height: 1)
                .foregroundColor(phosphor.opacity(0.45)),
            alignment: .bottom
        )
        .cornerRadius(6)
        .shadow(color: .black.opacity(0.6), radius: 10, x: 0, y: 4)
    }
}
