from datetime import datetime, timezone

from scripts.discord_backfill import (
    build_message_content,
    filter_new_rows,
    parse_channel_specs,
    to_ingest_row,
)


def test_parse_channel_specs_uses_defaults():
    targets = parse_channel_specs([])
    assert len(targets) == 7
    assert targets[0].channel_id
    assert targets[0].agent_id


def test_to_ingest_row_maps_bot_to_assistant_and_keeps_timestamp():
    row = to_ingest_row(
        "legaly",
        {
            "content": "답변입니다",
            "timestamp": "2026-04-26T12:34:56.000000+00:00",
            "author": {"bot": True},
            "attachments": [],
            "embeds": [],
        },
    )
    assert row == {
        "agent_id": "legaly",
        "role": "assistant",
        "content": "답변입니다",
        "created_at": "2026-04-26T12:34:56.000000+00:00",
    }


def test_build_message_content_includes_attachments_and_embeds():
    content = build_message_content(
        {
            "content": "본문",
            "attachments": [{"url": "https://cdn.example/file.txt"}],
            "embeds": [{"title": "제목", "description": "설명"}],
        }
    )
    assert "본문" in content
    assert "Attachments:" in content
    assert "https://cdn.example/file.txt" in content
    assert "Embed title: 제목" in content
    assert "Embed description: 설명" in content


def test_filter_new_rows_skips_old_or_equal_created_at():
    latest = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    rows = [
        {
            "agent_id": "cap",
            "role": "user",
            "content": "old",
            "created_at": "2026-04-26T11:59:59+00:00",
        },
        {
            "agent_id": "cap",
            "role": "user",
            "content": "equal",
            "created_at": "2026-04-26T12:00:00+00:00",
        },
        {
            "agent_id": "cap",
            "role": "assistant",
            "content": "new",
            "created_at": "2026-04-26T12:00:01+00:00",
        },
    ]
    filtered = filter_new_rows(rows, latest)
    assert [row["content"] for row in filtered] == ["new"]
