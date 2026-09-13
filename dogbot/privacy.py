import hashlib
import hmac
import secrets
import time
import asyncio
import logging
from .config import *


class PrivacyMixin:
    """Consent, stable account IDs, password authentication and 15-message context batches."""

    async def privacy_init(self):
        await self.db.init_privacy()
        self.pending_auth = {}
        # nick.casefold() -> {"user_id": ..., "since": monotonic_ts}
        # Живёт, пока ник непрерывно онлайн в комнате и не истёк
        # SESSION_MAX_AGE_SECONDS. Сбрасывается при disconnect/leave.
        self.authenticated_sessions = {}
        self.context_task = asyncio.create_task(self.context_batch_loop())

    async def require_consent(self, nick, reply_jid):
        if not await self.db.has_consent_by_nick(nick):
            self.send_message(mto=reply_jid, mbody=(
                "🔒 Сначала нужно согласие на обработку данных.\n"
                "В общем чате введи: !согласие\n"
                "После этого: !регистрация"
            ), mtype="chat")
            return False
        return True

    async def start_registration(self, nick, reply_jid):
        if await self.db.has_consent_by_nick(nick) is False:
            self.send_message(mto=reply_jid, mbody="Сначала введи !согласие в общем чате.", mtype="chat")
            return
        account = await self.db.register_account(nick)
        if account.get("existing"):
            text = (
                "ℹ️ Этот ник уже привязан к зарегистрированному аккаунту.\n"
                f"ID: {account['user_id']}\n"
                "Новый токен не выдаётся. Если забыл токен — попроси "
                "!сменить_токен, находясь в активной сессии, либо "
                "обратись к администратору."
            )
        else:
            token = f"{account['user_id']}:{account['password']}"
            text = (
                "✅ Регистрация завершена.\n"
                f"Твой токен доступа:\n{token}\n\n"
                "Сохрани его вне чата — это единственный ключ к аккаунту, "
                "восстановить нельзя. Хранится у Пса только в виде хэша.\n\n"
                "Как пользоваться:\n"
                "1. В общий чат — просто команда, например: !баланс\n"
                "2. Пёс спросит токен в личном сообщении — пришли строку "
                "целиком в ответ.\n"
                "Дальше, пока ты онлайн в комнате, токен повторно "
                "спрашивать не будет — сессия держится, пока не "
                "разорвётся соединение с комнатой."
            )
        self.send_message(mto=reply_jid, mbody=text, mtype="chat")

    async def accept_consent(self, nick, reply_jid):
        existing = await self.db.get_account_by_nick(nick)
        if existing:
            await self.db.set_consent(existing["user_id"], True)
            await self.db.bind_nick(existing["user_id"], nick)
            msg = "✅ Согласие сохранено для твоего аккаунта."
        else:
            await self.db.create_consent_record(nick)
            msg = "✅ Согласие записано. Теперь введи !регистрация в общем чате."
        self.send_message(mto=reply_jid, mbody=msg, mtype="chat")

    def _active_session(self, nick):
        """Возвращает валидную сессию для ника или None, чистит протухшие."""
        key = nick.casefold()
        session = self.authenticated_sessions.get(key)
        if not session:
            return None
        if time.monotonic() - session["since"] > SESSION_MAX_AGE_SECONDS:
            self.authenticated_sessions.pop(key, None)
            return None
        return session

    async def dispatch_command(self, nick, raw_text, room_jid):
        """
        Точка входа для любой "!команды" из общего чата.

        В сообщении в чат больше НЕ передаётся ID/пароль — только сама
        команда и её собственные аргументы (это раньше ломало команды
        из нескольких слов вроде "!мое досье", у которых второе слово
        путалось с ID).

        Если для текущего ника уже есть активная сессия (токен введён
        и присутствие в комнате не прерывалось) — команда выполняется
        сразу. Иначе Пёс просит токен в личку.
        """
        parts = self.parse_quoted_args(raw_text)
        if not parts:
            return True

        command_text = parts[0]
        args = parts[1:]
        reply_jid = f"{room_jid}/{nick}"

        session = self._active_session(nick)
        sensitive = command_text.casefold() in SENSITIVE_COMMANDS

        if session and not sensitive:
            await self.handle_commands(
                session["user_id"],
                " ".join([command_text] + args),
                reply_jid,
            )
            return True

        self.pending_auth[nick.casefold()] = {
            "nick": nick,
            "command": command_text,
            "args": args,
            "room_jid": room_jid,
            "created": time.monotonic(),
        }
        self.send_message(mto=reply_jid, mbody=(
            "🔐 Введи токен доступа в личку (ответом на это "
            f"сообщение), чтобы подтвердить «{command_text}».\n"
            "Токен выдаётся при !регистрация."
        ), mtype="chat")
        return True

    async def handle_auth_token(self, msg):
        """Принимает токен, отправленный в личку, и выполняет отложенную команду."""
        body = str(msg["body"] or "").strip()
        if not body:
            return False
        sender = str(msg["from"])
        nick = sender.split("/", 1)[1] if "/" in sender else (msg["from"].resource or sender)
        key = nick.casefold()
        pending = self.pending_auth.get(key)
        if not pending or time.monotonic() - pending["created"] > AUTH_TOKEN_TIMEOUT:
            self.pending_auth.pop(key, None)
            return False

        user_id = await self.db.verify_token(body)
        if not user_id:
            self.send_message(mto=sender, mbody="❌ Неверный токен. Запрос команды отменён.", mtype="chat")
            self.pending_auth.pop(key, None)
            return True

        self.pending_auth.pop(key, None)

        if not await self.db.has_consent(user_id):
            self.send_message(mto=sender, mbody="Для этого аккаунта нет действующего согласия. Введи !согласие.", mtype="chat")
            return True

        if not await self.db.bind_nick(user_id, pending["nick"]):
            self.send_message(mto=sender, mbody="❌ Не удалось привязать этот ник к аккаунту.", mtype="chat")
            return True

        # Токен подтверждён — открываем/продлеваем сессию для этого
        # ника, пока присутствие в комнате не прервётся.
        self.authenticated_sessions[key] = {
            "user_id": user_id,
            "since": time.monotonic(),
        }

        text = " ".join([pending["command"]] + pending["args"])
        await self.handle_commands(user_id, text, sender)
        return True

    def clear_session(self, nick):
        """Сбрасывает сессию и незавершённый auth-челлендж для ника."""
        key = nick.casefold()
        self.authenticated_sessions.pop(key, None)
        self.pending_auth.pop(key, None)

    async def on_muc_presence(self, presence):
        """
        При обрыве присутствия в комнате (дисконнект, выход, смена
        ника) сбрасываем сессию — дальше снова нужен токен. Это и есть
        фиксация "пока онлайн — токен не нужен, разорвал сессию —
        вводи заново".
        """
        try:
            if presence["from"].bare != self.room:
                return
            nick = presence["from"].resource
            if not nick:
                return
            if presence["type"] == "unavailable":
                self.clear_session(nick)
        except Exception:
            logging.exception("[СЕССИЯ] Ошибка обработки presence")

    async def revoke_consent(self, user_id, reply_jid):
        await self.db.revoke_consent(user_id)
        self.send_message(mto=reply_jid, mbody=(
            "🛑 Согласие отозвано. Новые сообщения этого аккаунта больше не"
            " записываются и не отправляются в LLM."
        ), mtype="chat")

    async def context_batch_loop(self):
        while True:
            try:
                await asyncio.sleep(CONTEXT_BATCH_CHECK_SECONDS)
                batch = await self.db.claim_message_batch(CONTEXT_BATCH_SIZE)
                if not batch:
                    continue
                try:
                    await self.process_passive_context_batch(batch)
                    await self.db.mark_message_batch(batch[0]["batch_id"], True)
                except Exception:
                    logging.exception("[ПАМЯТЬ] Ошибка фоновой обработки пачки")
                    await self.db.release_message_batch(batch[0]["batch_id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("[ПАМЯТЬ] Ошибка фонового цикла")

    async def process_passive_context_batch(self, batch):
        # Повторно проверяем согласие непосредственно перед передачей.
        # Если человек отозвал согласие после постановки сообщения в очередь,
        # его сообщения не попадут в LLM.
        allowed_batch = []
        for message in batch:
            if await self.db.has_consent(message["user_id"]):
                allowed_batch.append(message)
        if not allowed_batch:
            return

        context = "\n".join(
            f"[{m['user_id']}] {m['nickname']}: {m['body']}" for m in allowed_batch
        )
        raw = await self.call_openrouter([
            {"role": "system", "content": (
                "Ты фоновый аналитик памяти игрового XMPP-бота. "
                "Проанализируй ровно эту пачку сообщений. "
                "Выделяй только обычные, полезные и не секретные факты "
                "о самих авторах: интересы, хобби, предпочтения, работа, навыки, "
                "обычные события. Не извлекай пароли, токены, API-ключи, приватные ключи, "
                "секреты, чужие тайны или чувствительные сведения. "
                "Если это отказ/просьба/граница (например, не любит X, просил не "
                "называть его Y) — ставь \"negative\":true, такие факты не забываются. "
                "Верни только JSON вида {\"users\":[{\"user_id\":\"...\",\"facts\":["
                "{\"fact\":\"...\",\"category\":\"INFO\",\"importance\":5,"
                "\"confidence\":0.8,\"negative\":false}]}]}."
            )},
            {"role": "user", "content": context},
        ], json_mode=True, timeout=30, rate_limit=False)
        parsed = extract_json_object(raw) if raw else None
        if not isinstance(parsed, dict):
            return
        for item in parsed.get("users", []):
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id", "")).strip()
            if not user_id or not await self.db.has_consent(user_id):
                continue
            await self.db.add_facts(user_id, item.get("facts", []))
