"""Generate tiny, entirely synthetic Moect inputs in a test's temporary directory."""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import zstandard
from PIL import Image, ImageDraw


GROUP = "fixture@chatroom"
ALICE = "wxid_synthetic_alice"
BOB = "wxid_synthetic_bob"
DIRECT = "wxid_synthetic_direct"
STAMP = 1_800_000_000


def _table(session: str) -> str:
    return "Msg_" + hashlib.md5(session.encode()).hexdigest()


def sqlite_fixture(root: Path, *, damaged_timestamp: bool = False) -> dict:
    for directory in ("contact", "session", "message"):
        (root / directory).mkdir(parents=True)
    with closing(sqlite3.connect(root / "contact/contact.db")) as db, db:
        db.execute("""CREATE TABLE contact (
            id INTEGER, username TEXT, local_type INTEGER, alias TEXT, remark TEXT,
            nick_name TEXT, big_head_url TEXT, small_head_url TEXT, description TEXT)""")
        db.executemany("INSERT INTO contact VALUES (?, ?, 0, '', '', ?, '', '', '')", [
            (1, ALICE, "Alice"), (2, BOB, "Bob"), (3, GROUP, "Synthetic group"),
            (4, DIRECT, "Direct chat"),
        ])
    with closing(sqlite3.connect(root / "session/session.db")) as db, db:
        db.execute("CREATE TABLE SessionTable (username, last_timestamp, sort_timestamp, summary)")
        db.executemany("INSERT INTO SessionTable VALUES (?, ?, ?, 'synthetic')", [
            (GROUP, STAMP + 4, STAMP + 4), (DIRECT, STAMP, STAMP),
        ])

    quote = f"""<msg><appmsg><type>57</type><title>Answer</title><refermsg>
        <svrid>101</svrid><type>1</type><fromusr>{ALICE}</fromusr>
        <displayname>Alice</displayname><content>Compressed hello</content>
        </refermsg></appmsg></msg>"""
    forward = f"""<msg><appmsg><type>19</type><title>Thread</title><recorditem>
        <recordinfo><datalist>
          <dataitem datatype="5" dataid="note"><fromusr>{ALICE}</fromusr>
            <datadesc><![CDATA[{quote}]]></datadesc><srcMsgCreateTime>{STAMP - 60}</srcMsgCreateTime></dataitem>
          <dataitem datatype="17" dataid="nested"><datatitle>Nested</datatitle>
            <recordxml><recordinfo><datalist>
              <dataitem datatype="2" dataid="sticker"><fromusr>{BOB}</fromusr><datafmt>gif</datafmt></dataitem>
            </datalist></recordinfo></recordxml>
          </dataitem>
        </datalist></recordinfo></recorditem></appmsg></msg>"""
    shards = [
        [
            (1, "101", 1, 1, STAMP, zstandard.ZstdCompressor().compress(
                f"{ALICE}:\nCompressed hello".encode())),
            (2, "103", 49, 2, STAMP + 1, f"{BOB}:\n{quote}"),
            (3, "0", 3, 1, STAMP + 3, "<msg><img/></msg>"),
        ],
        [
            (1, "102", 1, 2, STAMP, "Note:\nKeep this prefix"),
            (2, "104", 49, 2, STAMP + 2, f"{BOB}:\n{forward}"),
            (3, "", 1, 1, STAMP + 4, "At the end"),
        ],
    ]
    if damaged_timestamp:
        shards[0].append((4, "bad-time", 1, 1, "invalid timestamp", "Unplaced message"))
    for shard, rows in enumerate(shards):
        with closing(sqlite3.connect(root / f"message/message_{shard}.db")) as db, db:
            db.execute("CREATE TABLE Name2Id (user_name TEXT)")
            db.executemany("INSERT INTO Name2Id VALUES (?)", [(ALICE,), (BOB,)])
            for session in (GROUP, DIRECT) if shard == 0 else (GROUP,):
                table = _table(session)
                db.execute(f"""CREATE TABLE {table} (
                    local_id INTEGER, server_id TEXT, local_type INTEGER, real_sender_id INTEGER,
                    create_time INTEGER, message_content BLOB, compress_content BLOB)""")
                messages = rows if session == GROUP else [(1, "201", 1, 2, STAMP, "Direct only")]
                db.executemany(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, NULL)", messages)
    return {"root": str(root), "wxid": ALICE}


def write_export(path: Path, metadata: dict, records: list[dict]) -> None:
    if path.suffix == ".json":
        content = json.dumps({"metadata": metadata, "messages": records, "count": len(records)})
    else:
        content = "".join(json.dumps(row) + "\n" for row in [{"metadata": metadata}, *records])
    path.write_text(content, encoding="utf-8")


def export_fixture(root: Path, suffix: str = ".jsonl") -> tuple[Path, dict, list[dict]]:
    path = root / ("synthetic" + suffix)
    assets = root / "synthetic_assets"
    resources = assets / "resources"
    resources.mkdir(parents=True)
    first = Image.new("RGB", (16, 12), "white")
    ImageDraw.Draw(first).rectangle((1, 1, 5, 5), fill="navy")
    first.save(resources / "static.png")
    first.resize((4, 3)).save(resources / "thumbnail.png")
    second = Image.new("RGB", first.size, "gold")
    first.save(resources / "animated.gif", save_all=True, append_images=[second], duration=100, loop=0)
    first.save(resources / "static.webp")
    (resources / "corrupt.png").write_bytes(b"synthetic broken image")
    outside = root.parent / "outside.png"
    first.save(outside)
    (resources / "escape.png").symlink_to(outside)

    def message(local_id, server_id, offset, kind="text", text="", **fields):
        return {
            "local_id": local_id, "server_id": server_id, "shard": 0,
            "create_time": STAMP + offset, "kind": kind, "text": text,
            "sender_wxid": ALICE, "sender": "Alice", "session": GROUP,
            "extra": {}, "resources": [], **fields,
        }

    def resource(filename, *, role="main", status="available", quality="original"):
        return {
            "kind": "image", "path": f"synthetic_assets/resources/{filename}",
            "name": filename, "role": role, "status": status, "quality": quality,
        }

    parent = message(4, "104", 3, "chatrecord", "Thread", forward={"messages": [
        message(0, "child-note", -30, text="Earlier note", sender_wxid=BOB),
        message(0, "child-nested", -20, "chatrecord", "Nested", forward={"messages": [
            message(0, "child-webp", -15, "image", resources=[resource("static.webp")]),
        ]}),
    ]})
    original = message(1, "101", 0, text="Hello")
    records = [
        original, dict(original, local_id=71, shard=1),
        message(2, "102", 0, "image", resources=[resource("static.png")]),
        message(3, "103", 1, "emoji", resources=[resource("animated.gif")]),
        parent, dict(parent, local_id=14, text="Thread variant"),
        message(5, "105", 2, "quote", "Answer", sender_wxid=BOB, quote=original),
        message(6, "106", 4, "image", resource_status="missing", resources=[
            resource("thumbnail.png", role="thumbnail"),
        ]),
        message(7, "107", 5, "image", resources=[resource("missing.png")]),
        message(8, "108", 6, "image", resources=[resource("corrupt.png")]),
        message(9, "109", 7, "image", resources=[dict(resource("unused"), path="../../etc/passwd")]),
        message(10, "110", 8, "image", resources=[resource("escape.png")]),
        message(11, "111", 9, "image"),  # status in resource-report.jsonl
        message(12, "112", 10, "voice", resource_status="unsupported"),
        message(13, "113", 11, "image", resource_status="skipped"),
        message(14, "0", 12, text="No server id"),
        message(15, "115", 13, "link", "Example", extra={
            "url": "https://example.invalid/article", "title": "Example", "source_name": "Fixture app",
        }),
    ]
    metadata = {"application": "Moect", "session": GROUP, "name": "Synthetic group", "time_zone": "Asia/Shanghai"}
    write_export(path, metadata, records)
    report = [
        {"message_id": "111", "kind": "image", "status": "encrypted",
         "image_diagnostics": {"reason": "aes_key_unavailable"}},
        {"message_id": "112", "kind": "voice", "status": "unsupported",
         "audio_diagnostics": {"reason": "invalid_silk_packets"}},
        {"message_id": "113", "kind": "image", "status": ALICE, "notice": BOB,
         "image_diagnostics": {"reason": ALICE}},
    ]
    (assets / "resource-report.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in report) + "{broken\n", encoding="utf-8",
    )
    return path, metadata, records
