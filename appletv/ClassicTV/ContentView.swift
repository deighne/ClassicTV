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

    private let phosphor = Color(red: 70/255, green: 1.0, blue: 122/255)

    var body: some View {
        VStack(spacing: 24) {
            Text("CLASSIC TV")
                .font(.system(size: 48, weight: .bold, design: .monospaced))
                .foregroundColor(phosphor)
            Text("Sign in with your Jellyfin account")
                .font(.headline)
                .foregroundColor(.secondary)

            VStack(spacing: 16) {
                TextField("Jellyfin Server URL", text: $serverURL)
                    .autocapitalization(.none)
                    .disableAutocorrection(true)
                TextField("Username", text: $username)
                    .autocapitalization(.none)
                    .disableAutocorrection(true)
                SecureField("Password", text: $password)
            }
            .frame(maxWidth: 620)

            HStack(spacing: 24) {
                Button {
                    Task { await vm.signIn(serverURL: serverURL, username: username, password: password) }
                } label: {
                    Text(vm.isLoggingIn ? "Signing In…" : "Tune In")
                        .frame(minWidth: 220)
                }
                .disabled(vm.isLoggingIn || username.isEmpty)

                Button {
                    if vm.quickConnectCode == nil {
                        vm.startQuickConnect(serverURL: serverURL)
                    } else {
                        vm.cancelQuickConnect()
                    }
                } label: {
                    Text(vm.quickConnectCode == nil ? "Quick Connect" : "Cancel Quick Connect")
                        .frame(minWidth: 240)
                }
                .disabled(vm.isLoggingIn)
            }

            if let qc = vm.quickConnectCode {
                VStack(spacing: 8) {
                    Text("ENTER CODE IN JELLYFIN → QUICK CONNECT")
                        .font(.caption.weight(.bold))
                        .foregroundColor(.secondary)
                    Text(qc)
                        .font(.system(size: 56, weight: .bold, design: .monospaced))
                        .foregroundColor(phosphor)
                        .tracking(8)
                }
                .padding(.top, 8)
            }

            if !vm.loginError.isEmpty {
                Text(vm.loginError)
                    .foregroundColor(.red)
                    .font(.callout)
            }
        }
        .padding(60)
    }
}

private struct TVStageView: View {
    @ObservedObject var vm: PlayerViewModel
    @FocusState private var stageFocused: Bool

    var body: some View {
        ZStack {
            VideoPlayerView(player: vm.player)
                .ignoresSafeArea()
                .focusable(!vm.drawerVisible)
                .focused($stageFocused)
                .onMoveCommand { direction in
                    switch direction {
                    case .up:
                        vm.stepChannel(1)
                    case .down:
                        vm.stepChannel(-1)
                    case .left:
                        vm.drawerVisible = true
                    case .right:
                        vm.showCurrentBug()
                    @unknown default:
                        break
                    }
                }
                .onPlayPauseCommand {
                    vm.drawerVisible.toggle()
                }

            if vm.showStatic {
                CRTStaticView()
            }

            if vm.bugVisible {
                VStack {
                    HStack {
                        Spacer()
                        ChannelBugView(number: vm.bugNumber, name: vm.bugName, title: vm.bugTitle)
                            .padding(.top, 48)
                            .padding(.trailing, 64)
                    }
                    Spacer()
                }
            }

            if vm.drawerVisible {
                ChannelDrawerView(vm: vm)
                    .transition(.move(edge: .leading))
                    .onExitCommand {
                        vm.drawerVisible = false
                    }
            }
        }
        .onAppear {
            stageFocused = true
        }
        .onChange(of: vm.drawerVisible) { _, open in
            if !open {
                stageFocused = true
            }
        }
    }
}

private struct ChannelDrawerView: View {
    @ObservedObject var vm: PlayerViewModel
    private let phosphor = Color(red: 70/255, green: 1.0, blue: 122/255)

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 20) {
                HStack {
                    Text("CHANNELS")
                        .font(.system(size: 32, weight: .bold, design: .monospaced))
                        .foregroundColor(phosphor)
                    Spacer()
                    Button("Guide") {
                        vm.drawerVisible = false
                        vm.tuneGuide()
                    }
                }

                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(Array(vm.channels.enumerated()), id: \.element.id) { idx, ch in
                            let num = vm.channelNumber(for: ch, index: idx)
                            Button {
                                vm.drawerVisible = false
                                vm.tune(index: idx)
                            } label: {
                                HStack(spacing: 16) {
                                    Text("\(num)")
                                        .font(.system(size: 26, weight: .bold, design: .monospaced))
                                        .foregroundColor(phosphor)
                                        .frame(width: 56, alignment: .trailing)
                                    Text(ch.name)
                                        .font(.system(size: 26, weight: .medium))
                                        .lineLimit(1)
                                    Spacer()
                                }
                                .padding(.vertical, 10)
                                .padding(.horizontal, 14)
                            }
                            .buttonStyle(.card)
                        }
                    }
                }

                HStack {
                    Button("Sign Out") {
                        vm.signOut()
                    }
                    Spacer()
                    Button("Close") {
                        vm.drawerVisible = false
                    }
                }
            }
            .padding(36)
            .frame(width: 580)
            .background(Color(red: 10/255, green: 14/255, blue: 16/255).opacity(0.95))
            .onExitCommand {
                vm.drawerVisible = false
            }

            Spacer()
        }
        .ignoresSafeArea()
    }
}
