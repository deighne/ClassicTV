import AVKit
import SwiftUI

struct VideoPlayerView: UIViewControllerRepresentable {
    let player: AVPlayer

    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let vc = AVPlayerViewController()
        vc.player = player
        vc.showsPlaybackControls = false
        vc.videoGravity = .resizeAspect
        return vc
    }

    func updateUIViewController(_ uiViewController: AVPlayerViewController, context: Context) {
        if uiViewController.player !== player {
            uiViewController.player = player
        }
    }
}

struct ContentView: View {
    @StateObject private var vm = PlayerViewModel()

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if vm.auth == nil {
                LoginView(vm: vm)
            } else {
                TVStageView(vm: vm)
            }
        }
    }
}

private struct LoginView: View {
    @ObservedObject var vm: PlayerViewModel
    @State private var serverURL = "https://jellyfin.example.com"
    @State private var username = ""
    @State private var password = ""
    @State private var copiedQc = false

    private let phosphor = Color(red: 70/255, green: 1.0, blue: 122/255)

    var body: some View {
        ScrollView {
            VStack(spacing: 20) {
                Spacer(minLength: 40)
                Text("CLASSIC TV")
                    .font(.system(size: 34, weight: .bold, design: .monospaced))
                    .foregroundColor(phosphor)
                Text("Sign in with your Jellyfin account")
                    .font(.subheadline)
                    .foregroundColor(.secondary)

                VStack(spacing: 14) {
                    TextField("Jellyfin Server URL", text: $serverURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .padding(14)
                        .background(Color.white.opacity(0.08))
                        .cornerRadius(10)

                    TextField("Username", text: $username)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .padding(14)
                        .background(Color.white.opacity(0.08))
                        .cornerRadius(10)

                    SecureField("Password", text: $password)
                        .padding(14)
                        .background(Color.white.opacity(0.08))
                        .cornerRadius(10)
                }
                .frame(maxWidth: 420)

                Button {
                    Task { await vm.signIn(serverURL: serverURL, username: username, password: password) }
                } label: {
                    Text(vm.isLoggingIn ? "Signing In…" : "Tune In")
                        .font(.headline)
                        .foregroundColor(.black)
                        .frame(maxWidth: 420)
                        .padding(.vertical, 14)
                        .background(phosphor)
                        .cornerRadius(10)
                }
                .disabled(vm.isLoggingIn || username.isEmpty)

                Button {
                    if vm.quickConnectCode == nil {
                        vm.startQuickConnect(serverURL: serverURL)
                    } else {
                        vm.cancelQuickConnect()
                    }
                } label: {
                    Text(vm.quickConnectCode == nil ? "Sign In with Quick Connect" : "Cancel Quick Connect")
                        .font(.subheadline.weight(.semibold))
                        .foregroundColor(phosphor)
                        .frame(maxWidth: 420)
                        .padding(.vertical, 12)
                        .background(Color.white.opacity(0.08))
                        .cornerRadius(10)
                }
                .disabled(vm.isLoggingIn)

                if let qc = vm.quickConnectCode {
                    Button {
                        UIPasteboard.general.string = qc
                        copiedQc = true
                        Task {
                            try? await Task.sleep(nanoseconds: 1_500_000_000)
                            copiedQc = false
                        }
                    } label: {
                        VStack(spacing: 6) {
                            Text(copiedQc ? "COPIED TO CLIPBOARD!" : "ENTER IN JELLYFIN → QUICK CONNECT (TAP TO COPY)")
                                .font(.caption2.weight(.bold))
                                .foregroundColor(copiedQc ? phosphor : .secondary)
                            Text(qc)
                                .font(.system(size: 36, weight: .bold, design: .monospaced))
                                .foregroundColor(phosphor)
                                .tracking(6)
                        }
                        .padding(.vertical, 10)
                        .padding(.horizontal, 18)
                        .background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 10))
                    }
                    .buttonStyle(.plain)
                }

                if !vm.loginError.isEmpty {
                    Text(vm.loginError)
                        .foregroundColor(.red)
                        .font(.footnote)
                        .multilineTextAlignment(.center)
                }
            }
            .padding(24)
        }
    }
}

private struct TVStageView: View {
    @ObservedObject var vm: PlayerViewModel
    @State private var showHud = true
    @State private var showNumpad = false
    @State private var showDrawerSheet = false

    private let phosphor = Color(red: 70/255, green: 1.0, blue: 122/255)

    var body: some View {
        GeometryReader { geo in
            let isWide = geo.size.width > geo.size.height
            Group {
                if isWide {
                    wideFullscreenStage
                } else {
                    portraitStage(width: geo.size.width)
                }
            }
            .frame(width: geo.size.width, height: geo.size.height)
        }
        .sheet(isPresented: $showNumpad) {
            NumpadSheet(vm: vm, isPresented: $showNumpad)
                .presentationDetents([.medium])
        }
        .sheet(isPresented: $showDrawerSheet) {
            ChannelListSheet(vm: vm, isPresented: $showDrawerSheet)
        }
    }

    private func videoBox(isWide: Bool) -> some View {
        ZStack {
            VideoPlayerView(player: vm.player)
            if vm.showStatic {
                CRTStaticView()
            }
            if vm.bugVisible {
                VStack {
                    HStack {
                        Spacer()
                        ChannelBugView(number: vm.bugNumber, name: vm.bugName, title: vm.bugTitle)
                            .padding(isWide ? 20 : 12)
                    }
                    Spacer()
                }
            }
        }
        .background(Color.black)
        .clipped()
        .contentShape(Rectangle())
        .onTapGesture {
            if isWide {
                withAnimation(.easeInOut(duration: 0.2)) {
                    showHud.toggle()
                }
            } else {
                vm.showCurrentBug()
            }
        }
        .gesture(
            DragGesture(minimumDistance: 32)
                .onEnded { value in
                    if value.translation.height < -40 {
                        vm.stepChannel(1)
                    } else if value.translation.height > 40 {
                        vm.stepChannel(-1)
                    }
                }
        )
    }

    private var wideFullscreenStage: some View {
        ZStack {
            videoBox(isWide: true).ignoresSafeArea()
            if showHud {
                VStack {
                    Spacer()
                    HStack(spacing: 16) {
                        Button("CH −") { vm.stepChannel(-1) }
                        Button("CH +") { vm.stepChannel(1) }
                        Button("Guide") { vm.tuneGuide() }
                        Button("Channels") { showDrawerSheet = true }
                        Button("123") { showNumpad = true }
                        Button(vm.isMuted ? "Unmute" : "Mute") { vm.toggleMute() }
                        Button("Sign Out") { vm.signOut() }
                    }
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(.white)
                    .padding(.horizontal, 18)
                    .padding(.vertical, 10)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.bottom, 18)
                }
            }
        }
    }

    private func portraitStage(width: CGFloat) -> some View {
        VStack(spacing: 0) {
            videoBox(isWide: false)
                .frame(width: width, height: width * 9 / 16)

            // Remote bar
            HStack(spacing: 8) {
                remoteButton(title: "CH −") { vm.stepChannel(-1) }
                remoteButton(title: "CH +") { vm.stepChannel(1) }
                remoteButton(title: "Guide") { vm.tuneGuide() }
                remoteButton(title: "123") { showNumpad = true }
                remoteButton(title: vm.isMuted ? "Unmute" : "Mute") { vm.toggleMute() }
                remoteButton(title: "Sign Out") { vm.signOut() }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 10)
            .background(Color(white: 0.08))

            // Channel Guide List
            List {
                ForEach(Array(vm.channels.enumerated()), id: \.element.id) { idx, ch in
                    let num = vm.channelNumber(for: ch, index: idx)
                    let isCurrent = idx == vm.currentIndex
                    Button {
                        vm.tune(index: idx)
                    } label: {
                        HStack(spacing: 12) {
                            Text("\(num)")
                                .font(.system(size: 18, weight: .bold, design: .monospaced))
                                .foregroundColor(phosphor)
                                .frame(width: 40, alignment: .trailing)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(ch.name)
                                    .font(.headline)
                                    .foregroundColor(.white)
                                if let now = vm.nowPlaying[ch.id], !now.isEmpty {
                                    Text(now)
                                        .font(.caption)
                                        .foregroundColor(.secondary)
                                        .lineLimit(1)
                                }
                            }
                            Spacer()
                            if isCurrent {
                                Image(systemName: "dot.radiowaves.left.and.right")
                                    .foregroundColor(phosphor)
                            }
                        }
                        .padding(.vertical, 4)
                    }
                    .listRowBackground(isCurrent ? phosphor.opacity(0.12) : Color.clear)
                }
            }
            .listStyle(.plain)
            .refreshable {
                await vm.refreshNowPlaying()
            }
        }
    }

    private func remoteButton(title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundColor(phosphor)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 10)
                .background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
        }
    }
}

private struct ChannelListSheet: View {
    @ObservedObject var vm: PlayerViewModel
    @Binding var isPresented: Bool
    private let phosphor = Color(red: 70/255, green: 1.0, blue: 122/255)

    var body: some View {
        NavigationStack {
            List {
                ForEach(Array(vm.channels.enumerated()), id: \.element.id) { idx, ch in
                    let num = vm.channelNumber(for: ch, index: idx)
                    Button {
                        isPresented = false
                        vm.tune(index: idx)
                    } label: {
                        HStack(spacing: 12) {
                            Text("\(num)")
                                .font(.system(size: 18, weight: .bold, design: .monospaced))
                                .foregroundColor(phosphor)
                                .frame(width: 40, alignment: .trailing)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(ch.name)
                                    .font(.headline)
                                if let now = vm.nowPlaying[ch.id], !now.isEmpty {
                                    Text(now)
                                        .font(.caption)
                                        .foregroundColor(.secondary)
                                        .lineLimit(1)
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("Channels")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { isPresented = false }
                }
            }
        }
    }
}

private struct NumpadSheet: View {
    @ObservedObject var vm: PlayerViewModel
    @Binding var isPresented: Bool
    private let phosphor = Color(red: 70/255, green: 1.0, blue: 122/255)
    private let rows = [
        ["1", "2", "3"],
        ["4", "5", "6"],
        ["7", "8", "9"],
        ["CLR", "0", "GO"]
    ]

    var body: some View {
        VStack(spacing: 14) {
            Text("DIRECT CHANNEL TUNE")
                .font(.caption.weight(.bold))
                .foregroundColor(phosphor)
                .padding(.top, 12)

            Text(vm.digitDisplay.isEmpty ? "Enter Channel #" : vm.digitDisplay)
                .font(.system(size: 20, weight: .bold, design: .monospaced))
                .foregroundColor(vm.digitDisplay.isEmpty ? .secondary : phosphor)
                .frame(minWidth: 240, minHeight: 36)
                .padding(.horizontal, 12)
                .background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))

            ForEach(rows, id: \.self) { row in
                HStack(spacing: 12) {
                    ForEach(row, id: \.self) { key in
                        if key == "CLR" {
                            Button {
                                vm.clearDigits()
                            } label: {
                                Text("Clear")
                                    .font(.callout.weight(.semibold))
                                    .frame(width: 76, height: 52)
                                    .background(Color.white.opacity(0.08))
                                    .foregroundColor(.secondary)
                                    .cornerRadius(10)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        } else if key == "GO" {
                            Button {
                                vm.commitDigits()
                                isPresented = false
                            } label: {
                                Text("Tune")
                                    .font(.callout.weight(.bold))
                                    .frame(width: 76, height: 52)
                                    .background(phosphor.opacity(0.22))
                                    .foregroundColor(phosphor)
                                    .cornerRadius(10)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        } else {
                            Button {
                                vm.enterDigit(key)
                            } label: {
                                Text(key)
                                    .font(.title2.weight(.bold))
                                    .frame(width: 76, height: 52)
                                    .background(Color.white.opacity(0.12))
                                    .foregroundColor(.white)
                                    .cornerRadius(10)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
        .padding()
        .onDisappear {
            vm.clearDigits()
        }
    }
}
