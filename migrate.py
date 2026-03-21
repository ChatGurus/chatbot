#!/usr/bin/env python3
"""
Convert ChatGPT export JSON directly to open-webui SQL insert statements.
No intermediate JSON files are written.
"""

import argparse
import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, List

# ── Hilfsfunktionen ─────────────────────────────────────────────────────────

INVALID_RE = re.compile(r"[\ue000-\uf8ff]")

def sanitize_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return INVALID_RE.sub("", text)

def _parts_to_text(parts: List[Any]) -> str:
    texts: List[str] = []
    for part in parts:
        if isinstance(part, str):
            texts.append(sanitize_text(part))
        elif isinstance(part, dict) and "text" in part:
            val = part.get("text")
            if isinstance(val, str):
                texts.append(sanitize_text(val))
    return "".join(texts)

def parse_timestamp(value: Any, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return default

# ── ChatGPT → interne Nachrichtenliste ──────────────────────────────────────

def parse_chatgpt(data: Any) -> List[dict]:
    conversations = data if isinstance(data, list) else [data]
    result = []

    for item in conversations:
        if not isinstance(item, dict):
            continue

        title = item.get("title") or item.get("name") or "Untitled"
        ts_raw = item.get("create_time") or item.get("update_time") or time.time()
        ts = parse_timestamp(ts_raw, time.time())
        conv_id = item.get("conversation_id") or item.get("id")

        messages: List[tuple[str, str, float]] = []

        # Fall 1: flache chat_messages Liste (neueres Format)
        if isinstance(item.get("chat_messages"), list):
            for idx, msg in enumerate(item["chat_messages"]):
                text = msg.get("text")
                if not text and isinstance(msg.get("content"), list):
                    text = _parts_to_text(msg["content"])
                text = sanitize_text(text)
                if text:
                    role = "user" if idx % 2 == 0 else "assistant"
                    messages.append((role, text, ts))

        # Fall 2: mapping-Baum (älteres Format)
        elif isinstance(item.get("mapping"), dict):
            mapping = item["mapping"]
            current_id = item.get("current_node")
            node = mapping.get(current_id) if current_id else None

            stack: List[tuple[str, str, float]] = []
            while isinstance(node, dict):
                msg = node.get("message") or {}
                parts = msg.get("content", {}).get("parts", [])
                if parts:
                    role = msg.get("author", {}).get("role", "assistant")
                    if role in {"user", "assistant"}:
                        ts_val = msg.get("create_time") or msg.get("timestamp") or ts
                        text = sanitize_text(_parts_to_text(parts))
                        if text:
                            stack.append((role, text, parse_timestamp(ts_val, ts)))

                parent_id = node.get("parent")
                if not parent_id:
                    break
                node = mapping.get(parent_id)

            messages.extend(reversed(stack))

        # Fallback: nur Titel als User-Nachricht
        if not messages:
            messages.append(("user", title, ts))

        result.append({
            "title": title,
            "timestamp": ts,
            "messages": messages,
            "conversation_id": conv_id,
        })

    return result

# ── open-webui Chat-Struktur (nur im Speicher) ─────────────────────────────

MODEL = "openai/gpt-4o-mini"   # ← hier ggf. ändern
MODEL_NAME = "GPT-4o mini"

def build_openwebui_chat(conversation: dict, user_id: str) -> dict:
    messages_map: dict = {}
    messages_list: List[dict] = []
    prev_id: str | None = None

    for role, content, ts in conversation["messages"]:
        msg_id = str(uuid.uuid4())
        clean = sanitize_text(content)

        msg = {
            "id": msg_id,
            "parentId": prev_id,
            "childrenIds": [],
            "role": role,
            "content": clean,
            "timestamp": int(ts),
        }

        if role == "user":
            msg["models"] = [MODEL]
        else:
            msg.update({
                "model": MODEL,
                "modelName": MODEL_NAME,
                "modelIdx": 0,
                "userContext": None,
                "lastSentence": clean.strip()[-120:].strip(),  # sehr vereinfacht
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "done": True,
            })

        if prev_id:
            messages_map[prev_id]["childrenIds"].append(msg_id)
        messages_map[msg_id] = msg
        messages_list.append(msg)
        prev_id = msg_id

    return {
        "id": "",
        "title": conversation["title"],
        "models": [MODEL],
        "params": {},
        "history": {"messages": messages_map, "currentId": prev_id},
        "messages": messages_list,
        "tags": [],
        "timestamp": int(conversation["timestamp"] * 1000),
        "files": [],
        "userId": user_id,
    }

# ── SQL-Teil ────────────────────────────────────────────────────────────────

def escape_sql_string(value: str) -> str:
    return value.replace("'", "''")

def build_meta(tags: list[str]) -> str:
    meta = json.dumps({"tags": tags}, ensure_ascii=True)
    return escape_sql_string(meta)

def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-")

def tag_upserts(user_id: str, tags: list[str]) -> list[str]:
    base_tags = [
        ("imported-chatgpt", "Imported from ChatGPT"),
    ]
    seen = {}
    for t in tags + [bt[1] for bt in base_tags]:
        slug = slugify(t)
        if slug not in seen:
            seen[slug] = t

    stmts = []
    for tag_id, name in seen.items():
        stmts.append(
            'INSERT INTO public.tag ("id","name","user_id","meta") '
            f"VALUES ('{tag_id}','{escape_sql_string(name)}','{user_id}',NULL) "
            'ON CONFLICT("id","user_id") DO UPDATE SET "name"=excluded."name";'
        )
    return stmts

def chat_to_sql(chat: dict, user_id: str, tags: list[str]) -> str:
    chat_json = json.dumps(chat, ensure_ascii=True)
    chat_json = escape_sql_string(chat_json)

    title = escape_sql_string(chat.get("title", "Untitled"))
    timestamp_ms = chat.get("timestamp", 0)
    created_at = int(timestamp_ms // 1000)

    # ID versuchen aus Dateinamen zu retten, sonst neu
    record_id = str(uuid.uuid4())

    meta = build_meta(tags)

    sql = (
        f"DELETE FROM public.chat WHERE \"id\" = '{record_id}';\n"
        "INSERT INTO public.chat "
        "(\"id\", \"user_id\", \"title\", \"share_id\", \"archived\", \"created_at\", \"updated_at\", \"chat\", \"pinned\", \"meta\", \"folder_id\")\n"
        f"VALUES ('{record_id}', '{user_id}', '{title}', NULL, false, {created_at}, {created_at}, '{chat_json}'::jsonb, false, '{meta}'::jsonb, NULL);\n"
    )
    return sql

# ── Hauptprogramm ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ChatGPT → open-webui SQL (no JSON files)")
    parser.add_argument("files", nargs="+", help="ChatGPT JSON file(s) or directory")
    parser.add_argument("--userid", required=True, help="User ID for all imported chats")
    parser.add_argument("--tags", default="imported-chatgpt", help="Comma-separated tags")
    args = parser.parse_args()

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    # Dateien sammeln
    input_files = []
    for p in args.files:
        if os.path.isdir(p):
            for name in os.listdir(p):
                if name.lower().endswith((".json", ".jsonl")):
                    input_files.append(os.path.join(p, name))
        elif os.path.isfile(p):
            input_files.append(p)

    if not input_files:
        print("Keine JSON-Dateien gefunden.", file=sys.stderr)
        return 1

    all_sql = []
    user_ids = {args.userid}  # in dieser Variante nur eine feste user_id

    for path in input_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            convs = parse_chatgpt(data)

            for conv in convs:
                chat = build_openwebui_chat(conv, args.userid)
                sql = chat_to_sql(chat, args.userid, tags)
                all_sql.append(sql)
                print(f"→ Verarbeitet: {conv['title']}", file=sys.stderr)

        except Exception as e:
            print(f"Fehler bei {path}: {e}", file=sys.stderr)

    # Tag-Upserts + alle Inserts
    prefix = []
    for uid in sorted(user_ids):
        prefix.extend(tag_upserts(uid, tags))

    final_sql = "\n".join(prefix + all_sql).rstrip() + "\n"

    output_file = "import.sql"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(final_sql)

    print(f"→ {len(all_sql)} Chats in import.sql geschrieben", file=sys.stderr)

if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)