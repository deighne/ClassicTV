import Foundation

struct AuthSession: Codable, Equatable {
    let serverURL: String
    let tvBaseURL: String
    let accessToken: String
    let userId: String
    let username: String
    let deviceId: String
}

struct ChannelInfo: Codable, Identifiable, Hashable {
    let id: String
    let name: String
    let number: Int
    let guide: Bool
    let mirror: Bool

    enum CodingKeys: String, CodingKey {
        case id = "Id"
        case name = "Name"
        case number = "Number"
        case guide = "Guide"
        case mirror = "Mirror"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? id
        number = try c.decodeIfPresent(Int.self, forKey: .number) ?? 0
        guide = try c.decodeIfPresent(Bool.self, forKey: .guide) ?? false
        mirror = try c.decodeIfPresent(Bool.self, forKey: .mirror) ?? false
    }
}

struct ChannelsResponse: Codable {
    let channels: [ChannelInfo]

    enum CodingKeys: String, CodingKey {
        case channels = "Channels"
    }
}

struct ChannelSlot: Codable {
    let channelId: String?
    let title: String
    let episodeTitle: String?
    let jellyfinItemId: String?
    let startOffset: Double?
    let duration: Double?
    let paused: Bool?

    enum CodingKeys: String, CodingKey {
        case channelId = "ChannelId"
        case title = "Title"
        case episodeTitle = "EpisodeTitle"
        case jellyfinItemId = "JellyfinItemId"
        case startOffset = "StartOffset"
        case duration = "Duration"
        case paused = "Paused"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        channelId = try c.decodeIfPresent(String.self, forKey: .channelId)
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        episodeTitle = try c.decodeIfPresent(String.self, forKey: .episodeTitle)
        jellyfinItemId = try c.decodeIfPresent(String.self, forKey: .jellyfinItemId)
        startOffset = try c.decodeIfPresent(Double.self, forKey: .startOffset)
        duration = try c.decodeIfPresent(Double.self, forKey: .duration)
        paused = try c.decodeIfPresent(Bool.self, forKey: .paused)
    }
}

struct AuthenticateResponse: Codable {
    struct User: Codable {
        let id: String
        let name: String

        enum CodingKeys: String, CodingKey {
            case id = "Id"
            case name = "Name"
        }
    }

    let accessToken: String
    let user: User

    enum CodingKeys: String, CodingKey {
        case accessToken = "AccessToken"
        case user = "User"
    }
}

struct PlaybackInfoResponse: Codable {
    struct MediaSource: Codable {
        let id: String
        let supportsDirectPlay: Bool?
        let transcodingUrl: String?

        enum CodingKeys: String, CodingKey {
            case id = "Id"
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
