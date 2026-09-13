from .config import *


class NicknameMixin:
    """
    Автономная выдача кличек.

    Два входа в одну и ту же логику (evaluate_and_maybe_rename_user):

      1. nickname_assigner_loop — фоновый цикл. Раз в
         NICKNAME_CHECK_TICK_SECONDS дозированно (NICKNAME_BATCH_SIZE
         за тик) проверяет самых "просроченных" пользователей и для
         тех, у кого действительно истёк персональный интервал
         (NICKNAME_MIN..MAX_INTERVAL_DAYS, перебрасывается заново
         каждый раз), спрашивает LLM.

      2. handle_admin_nickname_command — та же проверка, но
         немедленно и для конкретного пользователя, по команде
         администратора. Принимается ТОЛЬКО в личном сообщении:
         команда и токен никогда не звучат в общем чате и, как и
         любое содержимое личных сообщений, не пишутся в БД — эта
         функция сверяет токен в памяти процесса и сразу его
         забывает; в БД в итоге попадает только новая кличка (через
         update_nickname), а не текст команды.
    """

    ADMIN_NICK_RE = re.compile(
        r"^!кличка_админ\s+(\S+)\s+(\S+)\s*$"
    )

    # ========================================================
    # ФОНОВЫЙ ЦИКЛ
    # ========================================================

    async def nickname_assigner_loop(self):
        while True:
            try:
                await asyncio.sleep(
                    NICKNAME_CHECK_TICK_SECONDS
                )

                await self.nickname_check_tick()

            except asyncio.CancelledError:
                raise

            except Exception:
                logging.exception(
                    "[КЛИЧКИ] Ошибка фонового цикла"
                )

    async def nickname_check_tick(self):
        candidates = await self.db.get_nickname_check_candidates(
            NICKNAME_BATCH_SIZE
        )

        if not candidates:
            return

        now = datetime.now()

        for candidate in candidates:

            user_id = candidate.get("user_id")

            try:
                last_update_raw = candidate.get(
                    "last_nick_update"
                )

                if not last_update_raw:
                    # Новый аккаунт — фиксируем точку отсчёта,
                    # без немедленной проверки в тот же тик.
                    await self.db.touch_nick_update(user_id)
                    continue

                try:
                    last_update = datetime.fromisoformat(
                        str(last_update_raw)
                    )
                except (ValueError, TypeError):
                    await self.db.touch_nick_update(user_id)
                    continue

                # Порог перебрасывается заново на каждом тике —
                # так и было заложено в исходном плане: не
                # персистентный, а "плавающий" интервал.
                days_threshold = random.randint(
                    NICKNAME_MIN_INTERVAL_DAYS,
                    NICKNAME_MAX_INTERVAL_DAYS,
                )

                if (now - last_update) < timedelta(
                    days=days_threshold
                ):
                    # Кандидаты отсортированы по давности
                    # проверки — раз этот ещё не созрел, следующие
                    # (более свежие) тем более не созрели.
                    break

                await self.evaluate_and_maybe_rename_user(
                    user_id,
                    candidate.get("nickname"),
                    candidate.get("reputation") or 0,
                    announce=True,
                )

                # Пауза между запросами к LLM внутри одной пачки,
                # чтобы не упереться в rate limit провайдера.
                await asyncio.sleep(
                    NICKNAME_BATCH_PAUSE_SECONDS
                )

            except Exception:
                logging.exception(
                    "[КЛИЧКИ] Ошибка проверки пользователя %s",
                    user_id,
                )

    # ========================================================
    # ОБЩАЯ ЛОГИКА ОЦЕНКИ / ВЫДАЧИ
    # ========================================================

    async def evaluate_and_maybe_rename_user(
        self,
        user_id,
        current_nickname,
        reputation,
        announce=True,
    ):
        """
        Прогоняет пользователя через LLM-оценку и, если тот
        заслужил, меняет кличку. Используется и фоновым циклом, и
        админ-командой.

        Возвращает результат ask_llm_for_nickname (см. llm.py) —
        None при сбое вызова LLM, иначе {"give_nickname": bool,
        "nickname": str, "reason": str}.
        """
        facts = await self.db.get_user_facts(
            user_id,
            limit=EXPLICIT_MEMORY_LIMIT,
        )

        result = await self.ask_llm_for_nickname(
            facts,
            reputation,
            current_nickname,
        )

        if result is None:
            # Сбой вызова LLM — таймер не трогаем, чтобы попытка
            # повторилась на следующем тике, а не откладывалась
            # ещё на полный интервал.
            return None

        if result["give_nickname"]:

            new_nick = result["nickname"]

            await self.db.update_nickname(
                user_id,
                new_nick,
            )

            logging.info(
                "[КЛИЧКИ] %s: новая кличка «%s» (%s)",
                user_id,
                new_nick,
                result.get("reason", ""),
            )

            if announce:

                display = (
                    current_nickname
                    or await self.db.get_alias_for_user(user_id)
                    or "Кто-то из присутствующих"
                )

                self.send_message(
                    mto=self.room,
                    mbody=(
                        "🐾 Пёс всё видит и ничего не забывает.\n"
                        f"{display} отныне известен как "
                        f"«{new_nick}».\n"
                        f"Причина: {result.get('reason') or '—'}"
                    ),
                    mtype="groupchat",
                )

        else:
            # Кличку не меняем, но таймер сдвигаем — иначе Пёс
            # будет спрашивать про этого пользователя на каждом
            # следующем тике вместо полного интервала.
            await self.db.touch_nick_update(user_id)

        return result

    # ========================================================
    # АДМИН-КОМАНДА (ТОЛЬКО ЛИЧКА)
    # ========================================================

    async def handle_admin_nickname_command(self, msg):
        """
        Немедленная проверка/выдача клички конкретному
        пользователю по команде администратора.

        Вызывается из on_private_message ДО любой другой логики
        личных сообщений. Возвращает True, если сообщение было
        похоже на эту команду (обработано или отклонено с
        объяснением) — тогда on_private_message не должен
        обрабатывать его дальше. Возвращает False для любых
        сообщений, не начинающихся с этой команды, а также для
        отправителей не из списка операторов (чтобы не палить
        сам факт существования команды тем, кто не админ).
        """
        body = str(msg["body"] or "").strip()

        if not body.casefold().startswith("!кличка_админ"):
            return False

        sender_jid = str(msg["from"])

        nick = (
            msg["from"].resource
            or sender_jid.rsplit("/", 1)[-1]
        )

        if nick.casefold() not in BOT_ADMIN_NICKS:
            return False

        match = self.ADMIN_NICK_RE.match(body)

        if not match:
            self.send_message(
                mto=msg["from"],
                mbody=(
                    "Формат: !кличка_админ <ник_цели> "
                    "<admin_token>"
                ),
                mtype="chat",
            )
            return True

        target_nick, token = match.groups()

        if not ADMIN_TOKEN or not hmac.compare_digest(
            token, ADMIN_TOKEN
        ):
            self.send_message(
                mto=msg["from"],
                mbody="❌ Неверный админ-токен.",
                mtype="chat",
            )
            return True

        account = await self.db.get_account_by_nick(target_nick)

        if not account:
            self.send_message(
                mto=msg["from"],
                mbody=(
                    f"Не нашёл зарегистрированного «{target_nick}»."
                ),
                mtype="chat",
            )
            return True

        _, reputation = await self.db.get_user_info(
            account["user_id"]
        )

        result = await self.evaluate_and_maybe_rename_user(
            account["user_id"],
            account.get("nickname"),
            reputation,
            announce=True,
        )

        if result is None:
            self.send_message(
                mto=msg["from"],
                mbody="⚠️ LLM недоступна, попробуй позже.",
                mtype="chat",
            )
        elif result["give_nickname"]:
            self.send_message(
                mto=msg["from"],
                mbody=(
                    f"✅ {target_nick} теперь «{result['nickname']}». "
                    f"Причина: {result.get('reason', '')}"
                ),
                mtype="chat",
            )
        else:
            self.send_message(
                mto=msg["from"],
                mbody=(
                    f"Пёс посмотрел на досье {target_nick} и решил "
                    "кличку не менять."
                    + (
                        f" {result.get('reason')}"
                        if result.get("reason")
                        else ""
                    )
                ),
                mtype="chat",
            )

        return True
