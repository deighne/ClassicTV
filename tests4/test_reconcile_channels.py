from tools.reconcile_channels import fmt_uuid, norm_id, reconcile_config


def test_norm_and_fmt_uuid():
    raw = "40E2FA04-8875-041E-C55E-7F1775A20DB9"
    assert norm_id(raw) == "40e2fa048875041ec55e7f1775a20db9"
    assert fmt_uuid("40e2fa048875041ec55e7f1775a20db9") == "40e2fa04-8875-041e-c55e-7f1775a20db9"


def test_reconcile_replaces_dead_movie_and_series_ids():
    cfg = {
        "channels": [
            {
                "id": "ghiblet",
                "number": 22,
                "blocks": [
                    {
                        "name": "Ghibli",
                        "shows": [
                            {"name": "My Neighbor Totoro", "jellyfinIds": ["d20a86a7-11d4-8ac0-d909-68301ce77d20"]},
                            {"name": "Ponyo", "jellyfinIds": ["bad7677f-dc01-c38d-d8cb-8543794703b6"]},
                        ],
                    }
                ],
            },
            {
                "id": "sitcoms",
                "number": 15,
                "blocks": [
                    {
                        "name": "Prime",
                        "shows": [
                            {"name": "Seinfeld", "jellyfinIds": ["00000000-0000-0000-0000-000000000001"]},
                            {"name": "Missing Show", "jellyfinIds": ["00000000-0000-0000-0000-000000000002"]},
                        ],
                    }
                ],
            },
        ]
    }
    movies = [
        {"Id": "40e2fa048875041ec55e7f1775a20db9", "Name": "My Neighbor Totoro"},
        {"Id": "bad7677fdc01c38dd8cb8543794703b6", "Name": "Ponyo"},
    ]
    episodes = [
        {"Id": "11111111111111111111111111111112", "SeriesName": "Seinfeld", "ParentIndexNumber": 1, "IndexNumber": 2},
        {"Id": "11111111111111111111111111111111", "SeriesName": "Seinfeld", "ParentIndexNumber": 1, "IndexNumber": 1},
    ]

    updated, changes = reconcile_config(cfg, movies, episodes)
    assert [c["action"] for c in changes] == ["movie_replaced", "series_refreshed", "unresolved"]
    assert updated["channels"][0]["blocks"][0]["shows"][0]["jellyfinIds"] == [
        "40e2fa04-8875-041e-c55e-7f1775a20db9"
    ]
    assert updated["channels"][0]["blocks"][0]["shows"][1]["jellyfinIds"] == [
        "bad7677f-dc01-c38d-d8cb-8543794703b6"
    ]
    assert updated["channels"][1]["blocks"][0]["shows"][0]["jellyfinIds"] == [
        "11111111-1111-1111-1111-111111111111",
        "11111111-1111-1111-1111-111111111112",
    ]
