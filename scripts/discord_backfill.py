#!/usr/bin/env python3
"""
Backfill Discord channel history into the y-company conversations store.

Default flow:
1. Fetch paginated channel messages from Discord (100 per page, oldest-first replay).
2. Optionally query Supabase for the latest existing created_at per agent_id.
3. POST batches to /api/conversations with x-ingest-key auth.

Environment:
  DISCORD_BOT_TOKEN            Required unless --bot-token passed
  CONVERSATIONS_API_BASE_URL   Required unless --api-base-url passed
  CONVERSATIONS_INGEST_KEY     Required unless --ingest-key passed
  SUPABASE_URL                 Optional, enables created_at dedupe
  SUPABASE_KEY                 Optional, enables created_at dedupe

Examples:
  python scripts/discord_backfill.py --dry-run
  python scripts/discord_backfill.py --channel 1495963629798031431:counsely
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

DEFAULT_CHANNELS: dict[str, str] = {
    "1495963629798031431": "counsely",
    "1495963731035689000": "cap",
    "1495963774425764121": "bid",
    "1495964361578254427": "buildy",
    "1496118862104625283": "vital",
    "1496317610348777653": "growthy",
    "1497455009913245806": "legaly",
}


@dataclass(frozen=True)
class ChannelTarget:
    channel_id: str
    agent_id: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        help="channel_id:agent_id override; repeatable",
    )
    parser.add_argument("--bot-token", default=os.getenv("DISCORD_BOT_TOKEN", ""))
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("CONVERSATIONS_API_BASE_URL", ""),
        help="Base URL like https://example.com",
    )
    parser.add_argument(
        "--ingest-key",
        default=os.getenv("CONVERSATIONS_INGEST_KEY", ""),
        help="Value for x-ingest-key",
    )
    parser.add_argument(
        "--supabase-url",
        default=os.getenv("SUPABASE_URL", ""),
        help="Optional Supabase REST URL for dedupe reads",
    )
    parser.add_argument(
        "--supabase-key",
        default=os.getenv("SUPABASE_KEY", ""),
        help="Optional Supabase anon/service key for dedupe reads",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Discord page size and POST row batch size",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.25,
        help="Pause between Discord page fetches",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and transform only; do not POST",
    )
    return parser.parse_args()


def parse_channel_specs(raw_specs: list[str]) -> list[ChannelTarget]:
    if not raw_specs:
        return [
            ChannelTarget(channel_id=channel_id, agent_id=agent_id)
            for channel_id, agent_id in DEFAULT_CHANNELS.items()
        ]

    targets: list[ChannelTarget] = []
    for spec in raw_specs:
        if ":" not in spec:
            raise ValueError(f"Invalid --channel spec: {spec!r}")
        channel_id, agent_id = spec.split(":", 1)
        channel_id = channel_id.strip()
        agent_id = agent_id.strip()
        if not channel_id or not agent_id:
            raise ValueError(f"Invalid --channel spec: {spec!r}")
        targets.append(ChannelTarget(channel_id=channel_id, agent_id=agent_id))
    return targets


def build_message_content(message: dict[str, Any]) -> str:
    parts: list[str] = []
    content = (message.get("content") or "").strip()
    if content:
        parts.append(content)

    attachments = message.get("attachments") or []
    urls = [
        attachment.get("url", "").strip()
        for attachment in attachments
        if isinstance(attachment, dict)
    ]
    urls = [url for url in urls if url]
    if urls:
        parts.append("Attachments:\n" + "\n".join(urls))

    embeds = message.get("embeds") or []
    embed_lines: list[str] = []
    for embed in embeds:
        if not isinstance(embed, dict):
            continue
        title = (embed.get("title") or "").strip()
        description = (embed.get("description") or "").strip()
        if title:
            embed_lines.append(f"Embed title: {title}")
        if description:
            embed_lines.append(f"Embed description: {description}")
    if embed_lines:
        parts.append("\n".join(embed_lines))

    return "\n\n".join(parts).strip()


def to_ingest_row(agent_id: str, message: dict[str, Any]) -> dict[str, str] | None:
    content = build_message_content(message)
    if not content:
        return None

    author = message.get("author") or {}
    role = "assistant" if author.get("bot") else "user"
    created_at = message.get("timestamp")
    if not isinstance(created_at, str) or not created_at:
        return None

    return {
        "agent_id": agent_id,
        "role": role,
        "content": content,
        "created_at": created_at,
    }


def fetch_channel_messages(
    client: httpx.Client,
    channel_id: str,
    bot_token: str,
    page_size: int,
    pause_s: float,
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bot {bot_token}"}
    before: str | None = None
    all_messages: list[dict[str, Any]] = []

    while True:
        params = {"limit": str(page_size)}
        if before:
            params["before"] = before
        response = client.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers,
            params=params,
            timeout=30.0,
        )
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list) or not page:
            break
        all_messages.extend(page)
        before = page[-1]["id"]
        if len(page) < page_size:
            break
        time.sleep(pause_s)

    all_messages.reverse()
    return all_messages


def fetch_latest_created_at(
    client: httpx.Client,
    supabase_url: str,
    supabase_key: str,
    agent_id: str,
) -> datetime | None:
    if not supabase_url or not supabase_key:
        return None

    response = client.get(
        f"{supabase_url.rstrip('/')}/rest/v1/conversations",
        headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
        },
        params={
            "agent_id": f"eq.{agent_id}",
            "select": "created_at",
            "order": "created_at.desc",
            "limit": "1",
        },
        timeout=30.0,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or not rows:
        return None
    value = rows[0].get("created_at")
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def filter_new_rows(rows: list[dict[str, str]], latest_created_at: datetime | None) -> list[dict[str, str]]:
    if latest_created_at is None:
        return rows

    filtered: list[dict[str, str]] = []
    for row in rows:
        created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        if created_at > latest_created_at:
            filtered.append(row)
    return filtered


def post_rows(
    client: httpx.Client,
    api_base_url: str,
    ingest_key: str,
    rows: list[dict[str, str]],
    batch_size: int,
) -> int:
    inserted = 0
    for idx in range(0, len(rows), batch_size):
        batch = rows[idx : idx + batch_size]
        response = client.post(
            f"{api_base_url.rstrip('/')}/api/conversations",
            headers={"x-ingest-key": ingest_key},
            json={"rows": batch},
            timeout=60.0,
        )
        response.raise_for_status()
        payload = response.json()
        inserted += int(payload.get("inserted", 0))
    return inserted


def validate_required_args(args: argparse.Namespace) -> None:
    if not args.bot_token:
        raise SystemExit("Missing Discord bot token. Set DISCORD_BOT_TOKEN or pass --bot-token.")
    if not args.dry_run and not args.api_base_url:
        raise SystemExit("Missing API base URL. Set CONVERSATIONS_API_BASE_URL or pass --api-base-url.")
    if not args.dry_run and not args.ingest_key:
        raise SystemExit("Missing ingest key. Set CONVERSATIONS_INGEST_KEY or pass --ingest-key.")


def main() -> int:
    args = parse_args()
    validate_required_args(args)
    targets = parse_channel_specs(args.channel)

    summary: list[dict[str, Any]] = []
    with httpx.Client() as client:
        for target in targets:
            latest_created_at = fetch_latest_created_at(
                client,
                supabase_url=args.supabase_url,
                supabase_key=args.supabase_key,
                agent_id=target.agent_id,
            )
            messages = fetch_channel_messages(
                client,
                channel_id=target.channel_id,
                bot_token=args.bot_token,
                page_size=args.batch_size,
                pause_s=args.sleep,
            )
            rows = [
                row
                for message in messages
                if (row := to_ingest_row(target.agent_id, message)) is not None
            ]
            new_rows = filter_new_rows(rows, latest_created_at)

            inserted = 0
            if not args.dry_run and new_rows:
                inserted = post_rows(
                    client,
                    api_base_url=args.api_base_url,
                    ingest_key=args.ingest_key,
                    rows=new_rows,
                    batch_size=args.batch_size,
                )

            summary.append(
                {
                    "channel_id": target.channel_id,
                    "agent_id": target.agent_id,
                    "messages_fetched": len(messages),
                    "rows_built": len(rows),
                    "rows_after_dedupe": len(new_rows),
                    "inserted": inserted,
                    "latest_existing_created_at": (
                        latest_created_at.isoformat() if latest_created_at else None
                    ),
                }
            )

    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
