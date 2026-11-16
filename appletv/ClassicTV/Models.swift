import Foundation

struct AuthSession: Codable, Equatable {
    var serverURL: String
    var tvBaseURL: String
    var accessToken: String
    var userId: String
    var username: String
    var deviceId: String
}

struct ChannelInfo: Codable, Identifiable, Equatable {
    let id: String
    let name: String
    let number: Int
    let mirror: Bool

    enum CodingKeys: String, CodingKey {
        case id = "Id"
        case name = "Name"
        case number = "Number"
        case mirror = "Mirror"
    }
}

struct ChannelsResponse: Codable {
    let channels: [ChannelInfo]

    enum CodingKeys: String, CodingKey {
        case channels = "Channels"
    }
}

struct ChannelSlot: Codable, Equatable {
    let channelId: String
    let title: String
    let episodeTitle: String?
    let jellyfinItemId: String?
    let startOffset: Double?
    let duration: Double?
    let mirror: Bool?
    let paused: Bool?

    enum CodingKeys: String, CodingKey {
        case channelId = "ChannelId"
        case title = "Title"
        case episodeTitle = "EpisodeTitle"
        case jellyfinItemId = "JellyfinItemId"
        case startOffset = "StartOffset"
        case duration = "Duration"
        case mirror = "Mirror"
        case paused = "Paused"
    }
}

struct AuthenticateResponse: Codable {
    struct UserInfo: Codable {
        let id: String
        let name: String

        enum CodingKeys: String, CodingKey {
            case id = "Id"
            case name = "Name"
        }
    }

    let accessToken: String
    let user: UserInfo

    enum CodingKeys: String, CodingKey {
        case accessToken = "AccessToken"
        case user = "User"
    }
}

struct PlaybackInfoResponse: Codable {
    struct MediaSource: Codable {
        let id: String
        let container: String?
        let supportsDirectPlay: Bool?
        let transcodingUrl: String?

        enum CodingKeys: String, CodingKey {
            case id = "Id"
            case container = "Container"
            case supportsDirectPlay = "SupportsDirectPlay"
            case transcodingUrl = "TranscodingUrl"
        }
    }

    let mediaSources: [MediaSource]

    enum CodingKeys: String, CodingKey {
        case mediaSources = "MediaSources"
    }
}

struct QuickConnectResult: Codable {
    let authenticated: Bool
    let secret: String
    let code: String

    enum CodingKeys: String, CodingKey {
        case authenticated = "Authenticated"
        case secret = "Secret"
        case code = "Code"
    }
}
