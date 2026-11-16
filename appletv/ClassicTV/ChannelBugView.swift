import SwiftUI

struct ChannelBugView: View {
    let number: String
    let name: String
    let title: String

    private let phosphor = Color(red: 70/255, green: 1.0, blue: 122/255)

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 14) {
                Text(number)
                    .font(.system(size: 46, weight: .bold, design: .monospaced))
                    .foregroundColor(phosphor)
                    .shadow(color: phosphor.opacity(0.45), radius: 8)
                Text(name.uppercased())
                    .font(.system(size: 26, weight: .bold, design: .monospaced))
                    .foregroundColor(phosphor)
                    .lineLimit(1)
            }
            if !title.isEmpty {
                Text(title)
                    .font(.system(size: 22, weight: .regular, design: .monospaced))
                    .foregroundColor(Color(red: 228/255, green: 247/255, blue: 233/255))
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
        .frame(minWidth: 320, maxWidth: 540, alignment: .leading)
        .background(Color(red: 4/255, green: 16/255, blue: 8/255).opacity(0.9))
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .stroke(phosphor.opacity(0.4), lineWidth: 2)
        )
        .cornerRadius(6)
    }
}
