from .config import *

class DogMucMixin:

    def _send_dog_message(
        self,
        room_jid,
        body,
    ):
        """
        Отправляет сообщение от лица бота в комнату и синхронно
        обновляет self.history/self.quote_lookback — общая логика
        и для обычных LLM-ответов (process_llm_response), и для
        коротких заготовленных реплик (например, отказ из-за
        перегрузки — см. LoadDegradationTracker).
        """
        self.send_message(
            mto=room_jid,
            mbody=body,
            mtype="groupchat",
        )

        self.history.append(
            f"{self.nick}: {body}"
        )

        # Симметрично с обычными сообщениями участников: если
        # кто-то потом процитирует реальные слова Пса, атрибуция
        # должна находить именно его, а не "автора неизвестно".
        self.quote_lookback.append(
            {
                "sender": self.nick,
                "text": body,
                "stanza_id": None,
            }
        )

        # Короткий архив — источник исторического контекста. Записываем
        # и ответы Пса, чтобы старый диалог можно было восстановить
        # симметрично с репликами пользователей.
        if CHAT_ARCHIVE_ENABLED and hasattr(self, "db"):
            asyncio.create_task(
                self.db.record_chat_archive_message(
                    self.room,
                    self.nick,
                    None,
                    body,
                    user_id=None,
                    mentions=[],
                    quoted_text=None,
                    quote_author=None,
                    is_command=False,
                )
            )

    async def import_muc_history(self, messages):
        """
        Импортирует серверную MUC-историю, полученную при join_muc_wait().
        Исторические сообщения проходят тот же программный разбор цитат/
        упоминаний, но НИКОГДА не запускают consent, команды или LLM.
        """
        imported = 0
        for msg in messages or []:
            try:
                text = str(msg["body"] or "").strip()
                sender = str(msg["mucnick"] or "").strip()
                if not text or not sender or sender.casefold() == self.nick.casefold():
                    continue
                room_jid = msg["from"].bare
                quoted_text, author_text = self.parse_quote_block(text)
                mentioned_nicks = self.find_mentioned_nicks(
                    self.parse_mention(author_text)[1],
                    exclude={sender},
                )
                account = await self.db.resolve_user_by_nick(sender)
                quote_author = None
                # Для старой цитаты reply metadata может быть уже в stanza.
                if quoted_text:
                    quote_author = self.resolve_quote_author(msg, quoted_text)
                ok = await self.db.record_chat_archive_message(
                    room_jid,
                    sender,
                    self._mod_occupant_jid(room_jid, sender),
                    text,
                    created_at=self.extract_message_created_at(msg),
                    user_id=account["user_id"] if account else None,
                    stanza_id=self.extract_stanza_id(msg),
                    mentions=mentioned_nicks,
                    quoted_text=quoted_text,
                    quote_author=quote_author,
                    quote_stanza_id=(
                        self.extract_reply_target_nick(msg)[1]
                        if quoted_text else None
                    ),
                    is_command=text.startswith("!"),
                )
                if ok:
                    imported += 1
                    self.quote_lookback.append({
                        "sender": sender,
                        "sender_jid": self._mod_occupant_jid(room_jid, sender),
                        "text": text,
                        "stanza_id": self.extract_stanza_id(msg),
                    })
            except Exception:
                logging.exception("[АРХИВ] Ошибка импорта исторического MUC-сообщения")
        return imported

    async def import_mam_history(self):
        """
        Добирает MUC-историю через XEP-0313 MAM за retention-период.

        MAM возвращает result -> forwarded -> stanza; запись выполняется
        существующей БД с той же нормализацией, что и MUC history. Поэтому
        источники безопасно объединяются через текущую дедупликацию archive.
        """
        if not CHAT_ARCHIVE_MAM_ENABLED or not CHAT_ARCHIVE_ENABLED:
            return 0

        try:
            mam = self.plugin["xep_0313"]
        except Exception:
            logging.info("[АРХИВ/MAM] XEP-0313 недоступен — пропуск")
            return 0
        if not hasattr(mam, "retrieve"):
            return 0

        now = datetime.now(timezone.utc)
        start = now - timedelta(days=max(int(CHAT_ARCHIVE_RETENTION_DAYS), 1))
        end = now + timedelta(seconds=2)
        imported = 0
        seen = set()

        pages = mam.retrieve(
            jid=self.room,
            start=start,
            end=end,
            iterator=True,
            rsm={"max": max(int(CHAT_ARCHIVE_MAM_PAGE_SIZE), 1)},
            timeout=max(int(CHAT_ARCHIVE_MAM_TIMEOUT), 1),
        )

        async for page in pages:
            for result in page.get("mam", {}).get("results", []):
                try:
                    forwarded = result["mam_result"]["forwarded"]
                    msg = forwarded["stanza"]
                    text = str(msg["body"] or "").strip()
                    if not text:
                        continue

                    room_jid = msg["from"].bare
                    sender = str(msg["from"].resource or "").strip()
                    if not sender or sender.casefold() == self.nick.casefold():
                        continue

                    stanza_id = self.extract_stanza_id(msg)
                    if not stanza_id:
                        try:
                            stanza_id = result["mam_result"]["id"]
                        except Exception:
                            stanza_id = None

                    dedupe_key = (room_jid, stanza_id, sender, text)
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)

                    quoted_text, author_text = self.parse_quote_block(text)
                    mentioned_nicks = self.find_mentioned_nicks(
                        self.parse_mention(author_text)[1],
                        exclude={sender},
                    )
                    account = await self.db.resolve_user_by_nick(sender)
                    quote_author = None
                    if quoted_text:
                        quote_author = await self.resolve_quote_author_async(
                            room_jid, msg, quoted_text
                        )

                    ok = await self.db.record_chat_archive_message(
                        room_jid,
                        sender,
                        self._mod_occupant_jid(room_jid, sender),
                        text,
                        created_at=self.extract_message_created_at(msg),
                        user_id=account["user_id"] if account else None,
                        stanza_id=stanza_id,
                        mentions=mentioned_nicks,
                        quoted_text=quoted_text,
                        quote_author=quote_author,
                        quote_stanza_id=(
                            self.extract_reply_target_nick(msg)[1]
                            if quoted_text else None
                        ),
                        is_command=text.startswith("!"),
                    )
                    if ok:
                        imported += 1
                        self.quote_lookback.append({
                            "sender": sender,
                            "sender_jid": self._mod_occupant_jid(room_jid, sender),
                            "text": text,
                            "stanza_id": stanza_id,
                        })
                except Exception:
                    logging.exception("[АРХИВ/MAM] Ошибка обработки MAM-сообщения")

        return imported

    async def muc_message(
        self,
        msg,
    ):
        try:

            text = str(
                msg["body"] or ""
            ).strip()

            sender = str(
                msg["mucnick"]
            )

            if (
                not text
                or sender.casefold()
                == self.nick.casefold()
            ):
                return

            room_jid = (
                msg["from"].bare
            )

            logging.info(
                "[ЧАТ] %s: %s",
                sender,
                text,
            )

            # Анти-рейд/анти-бот модерация выполняется ДО регистрации,
            # памяти, экономики, команд и LLM. Если сообщение уже
            # обработано модератором, основной pipeline его не трогает.
            if await self.moderate_incoming_muc(
                msg,
                sender,
                text,
                room_jid,
            ):
                return

            reply_jid = f"{room_jid}/{sender}"

            # Считаем адресата пораньше (раньше это делалось только
            # ниже, в блоке HISTORY) — он нужен уже для разбора
            # упоминаний/цитаты ниже, до попадания сообщения в
            # quote_lookback.
            # СНАЧАЛА отделяем цитату, и только потом разбираем
            # обращение/упоминания в собственном тексте автора.
            # Иначе строка внутри цитаты вроде "> russoturisto: ..."
            # потенциально могла быть воспринята как реальный адресат
            # текущего сообщения. Любой ник внутри quoted_text — это
            # часть ЧУЖОЙ цитаты, а не обращение автора.
            quoted_text, author_text = self.parse_quote_block(text)
            target_nick, remainder_text = self.parse_mention(
                author_text
            )

            # remainder_text — единственный источник для адресации,
            # упоминаний, recall-target и триггеров. quoted_text
            # передаётся отдельно как контекст цитаты.
            clean_text = remainder_text

            # Все упоминания вычисляются ТОЛЬКО по собственному
            # тексту автора, уже после отделения quote-block.
            # Упоминание в середине фразы — это mention, а не
            # адресат и тем более не новый автор сообщения.
            mentioned_nicks = self.find_mentioned_nicks(
                clean_text,
                exclude={sender},
            )

            # Автор цитаты определяется ДО помещения текущего сообщения
            # в lookback — иначе текст мог бы ошибочно сопоставиться
            # с самим собой. Те же структурные метаданные сохраняем в
            # архиве, чтобы исторический контекст не терял авторство.
            quote_author = (
                await self.resolve_quote_author_async(room_jid, msg, quoted_text)
                if quoted_text
                else None
            )

            # Один ранний resolve используется и архивом, и основным
            # pipeline. Архив пишется независимо от регистрации: это
            # сознательно отличается от персональной памяти и
            # context_messages — retention всего 14 дней и нужен для
            # восстановления общего MUC-разговора, включая реплики
            # незарегистрированных участников.
            account = await self.db.resolve_user_by_nick(sender)
            archive_user_id = account["user_id"] if account else None

            # user_id остаётся None для любого, кто не зарегистрирован
            # (или отозвал согласие) — это единственный сигнал ниже по
            # пайплайну о том, что аккаунт-специфичные эффекты (личная
            # память/факты, репутация, игровая валюта) применять
            # нельзя. Сам разговор с Псом от user_id больше не зависит.
            user_id = archive_user_id

            if CHAT_ARCHIVE_ENABLED:
                await self.db.record_chat_archive_message(
                    room_jid,
                    sender,
                    self._mod_occupant_jid(room_jid, sender),
                    text,
                    user_id=archive_user_id,
                    stanza_id=self.extract_stanza_id(msg),
                    mentions=mentioned_nicks,
                    quoted_text=quoted_text,
                    quote_author=quote_author,
                    quote_stanza_id=(
                        self.extract_reply_target_nick(msg)[1]
                        if quoted_text
                        else None
                    ),
                    is_command=text.startswith("!"),
                )

            # ------------------------------------------------
            # CONSENT / REGISTRATION
            #
            # !согласие и !регистрация остаются доступны — они всё
            # ещё нужны тем, кто хочет личную память (факты),
            # репутацию и игровую валюту на постоянном аккаунте. Но
            # это больше не шлагбаум перед самим разговором: ниже Пёс
            # отвечает любому собеседнику независимо от того, есть ли
            # у него account.
            # ------------------------------------------------
            lower = text.casefold().strip()
            if lower == "!согласие":
                await self.accept_consent(sender, reply_jid)
                return

            if lower == "!регистрация":
                await self.start_registration(sender, reply_jid)
                return

            # !вайтлист сверяет права по РЕАЛЬНОМУ bare JID вызывающего
            # через occupant-метаданные MUC (_mod_occupant_jid ждёт
            # настоящий ник и голый room_jid, см. moderation.py). Через
            # общий dispatch_command команда пошла бы по токен-сессии,
            # которая подставляет вместо ника внутренний user_id, а
            # вместо голого room_jid — "room_jid/ник" (это нужно
            # экономике/токенам, но ломает поиск occupant'а). Поэтому
            # !вайтлист, как !согласие/!регистрация выше, обрабатывается
            # здесь напрямую, с настоящими sender/room_jid.
            if lower == "!вайтлист" or lower.startswith("!вайтлист "):
                await self.handle_moderation_whitelist_command(
                    sender, text, room_jid
                )
                return

            # ------------------------------------------------
            # EVERY OTHER COMMAND: только сама команда здесь,
            # токен доступа подтверждается в личке (dispatch_command
            # сам решит — по кэшу сессии или через новый запрос токена).
            # ------------------------------------------------
            if text.startswith("!"):
                await self.dispatch_command(sender, text, room_jid)
                return

            # ------------------------------------------------
            # ACCOUNT-ONLY SIDE EFFECTS
            #
            # Фоновый 15-сообщений батч для извлечения личных фактов
            # и пополнение банка — то, что реально "жёстко завязано
            # на регистрацию". Оба шага требуют настоящего user_id и
            # намеренно пропускаются для незарегистрированных —
            # иначе факты/валюта тихо заводились бы на нике вместо
            # аккаунта. Сам ответ LLM ниже от этого не зависит.
            # ------------------------------------------------
            if account:

                # Every ordinary message from a consented user is
                # persisted for the background 15-message LLM batch.
                # Passwords never enter this path because passwords
                # are accepted only through MUC private messages.
                await self.db.record_chat_message(
                    user_id, sender, text
                )

                inf = await self.db.get_inflation_multiplier()
                await self.db.add_to_bank(100.0 * inf)

            # ------------------------------------------------
            # QUIZ
            #
            # resolve_quiz_answer (core.py) — единая точка входа:
            # сначала бесплатный Python exact match, если не сработал
            # — LLM judge как второй уровень (см. ТЗ). Реальное
            # начисление награды в любом случае проходит через
            # атомарный try_win_quiz(). См. также on_private_message
            # в core.py — тот же путь для ответов в личку.
            # ------------------------------------------------

            quiz_result = await self.resolve_quiz_answer(
                sender, text
            )

            if quiz_result is not None:

                if quiz_result["status"] == "race_lost":
                    # Кто-то уже успел ответить раньше (гонка), или
                    # LLM judge отработал слишком долго и раунд успел
                    # смениться — молча считаем сообщение обработанным.
                    return

                if quiz_result["status"] == "no_account":
                    # Ответ верный, но аккаунт не привязан к нику.
                    # Викторина не закрывается — раунд остаётся
                    # открытым, чтобы приз не сгорал впустую.
                    self.send_message(
                        mto=reply_jid,
                        mbody=(
                            "🎯 Похоже, ответ верный! Но чтобы "
                            "получить награду, нужен "
                            "зарегистрированный аккаунт.\n"
                            "В общем чате: !согласие, затем "
                            "!регистрация. После этого напиши "
                            "ответ ещё раз (в чат или в личку) — "
                            "викторина остаётся открытой."
                        ),
                        mtype="chat",
                    )
                    return

                actual = quiz_result["reward"]

                if actual > 0:

                    self._send_dog_message(
                        room_jid,
                        (
                            f"🎯 БИНГО! {sender} "
                            f"первым ответил правильно "
                            f"и получил {actual:.2f} "
                            "коинов!"
                        ),
                    )

                else:

                    self._send_dog_message(
                        room_jid,
                        (
                            f"🎯 {sender} "
                            "ответил правильно, "
                            "но банк пуст."
                        ),
                    )

                return

            # ------------------------------------------------
            # IGNORED SENDERS
            # ------------------------------------------------

            if sender in IGNORED_SENDERS:
                return

            # ------------------------------------------------
            # COMMANDS
            # ------------------------------------------------

            if await self.handle_commands(
                sender,
                text,
                room_jid,
            ):
                return

            # ------------------------------------------------
            # RANDOM QUIZ
            # ------------------------------------------------

            if (
                not self.active_quiz
                and random.random() < 0.007
            ):

                asyncio.create_task(
                    self.start_llm_quiz(
                        room_jid
                    )
                )

            # ------------------------------------------------
            # HISTORY
            # ------------------------------------------------

            # target_nick/clean_text уже посчитаны выше (нужны были
            # раньше — см. блок NO CONSENT).

            # Цитаты ("> текст") сверяются и с self.quote_lookback,
            # и (внутри resolve_quote_author, в первую очередь) со
            # структурными метаданными ответа — и то, и другое ДО
            # того, как текущее сообщение туда попадёт, иначе автор
            # мог бы "процитировать сам себя" (см.
            # resolve_quote_author/find_quote_author). quoted_text/
            # remainder_text уже посчитаны выше (нужны были раньше —
            # см. блок ACCOUNT-ONLY SIDE EFFECTS).
            # quote_author уже определён выше, до попадания текущей
            # реплики в lookback, и одновременно сохранён в архиве.

            # quote_lookback и self.history пишутся для ВСЕХ, без
            # оглядки на регистрацию — оба идут в LLM как контекст
            # диалога; сюда же, симметрично, попадают и ответы самого
            # Пса — см. process_llm_response.
            self.quote_lookback.append(
                {
                    "sender": sender,
                    "sender_jid": self._mod_occupant_jid(
                        room_jid, sender
                    ),
                    "text": text,
                    "stanza_id": self.extract_stanza_id(msg),
                }
            )

            if target_nick:

                self.history.append(
                    f"{sender} "
                    f"(к {target_nick}): "
                    f"{remainder_text}"
                )

            else:

                self.history.append(
                    f"{sender}: {text}"
                )

            # lower/mention_russoturisto и is_addressed_to_me ниже
            # намеренно смотрят на remainder_text, а НЕ на текст
            # сообщения целиком — иначе слово "пёс" или ник
            # "russoturisto", встретившиеся в процитированной
            # (чужой) части сообщения, засчитывались бы как
            # обращение/упоминание от текущего автора, который их
            # не писал. Внутри цитаты триггеры срабатывать не
            # должны.
            lower = remainder_text.casefold()

            mention_russoturisto = any(
                str(nick).casefold() == "russoturisto"
                for nick in mentioned_nicks
            )

            trigger = False

            # is_addressed_to_me() централизует то же правило, что
            # раньше было продублировано здесь: явный адресат
            # ("Ник: ...") важнее случайного упоминания слова
            # "пёс"/"пес" где-то в тексте. Используется и выше, для
            # решения о приглашении незарегистрированных ников.
            is_direct_to_me = self.is_addressed_to_me(
                remainder_text, target_nick
            )

            if is_direct_to_me:

                trigger = True

            elif target_nick:

                trigger = (
                    random.random() < 0.05
                    if self.engaged
                    else random.random() < 0.01
                )

            else:

                trigger = (
                    random.random() < 0.05
                    if self.engaged
                    else random.random() < 0.02
                )

            if mention_russoturisto:

                # Раньше здесь ещё стояло `is_direct_to_me = False`.
                # Это ошибочно "забывало" факт прямого обращения к
                # боту и через resolve_context_needs() ->
                # context_precheck() ("not is_direct_to_me and not
                # target_nick" -> needs_history=False) лишало такие
                # сообщения истории чата — даже когда автор реально
                # обращался к Псу, просто заодно упомянув
                # russoturisto. Упоминание russoturisto само по себе
                # не должно понижать статус прямого обращения —
                # только гарантировать trigger=True.
                trigger = True

            if not trigger:
                return

            # ------------------------------------------------
            # LOAD DEGRADATION (защита от "бесплатной
            # техподдержки" — см. LoadDegradationTracker и блок
            # LOAD DEGRADATION в config.py)
            # ------------------------------------------------
            #
            # Считаем нагрузку по remainder_text (собственные
            # слова автора без цитаты) — по тем же причинам, что
            # и is_addressed_to_me/mention_russoturisto выше:
            # цитата с чужой технической просьбой не должна
            # засчитываться как злоупотребление от того, кто её
            # процитировал. account_id — тот же ключ, что и у
            # досье/репутации, чтобы уровень не сбрасывался при
            # смене ника.
            account_id = user_id or sender

            load_weight = (
                1.0
                if looks_like_work_request(
                    remainder_text
                )
                else LOAD_CHAT_WEIGHT
            )

            load_level = (
                await self.load_tracker.bump(
                    account_id,
                    weight=load_weight,
                )
            )

            if load_level >= LOAD_TIER_REFUSE:

                logging.info(
                    "[НАГРУЗКА] %s: уровень %.2f >= "
                    "%.2f — отказ без вызова LLM",
                    sender,
                    load_level,
                    LOAD_TIER_REFUSE,
                )

                self._send_dog_message(
                    room_jid,
                    random.choice(
                        LOAD_REFUSAL_REPLIES
                    ),
                )

                return

            # ------------------------------------------------
            # LLM RATE LIMIT
            # ------------------------------------------------

            allowed, reason = (
                await self.llm_rate_limiter.allow(
                    sender
                )
            )

            if not allowed:

                logging.warning(
                    "[LLM] Rate limit %s: %s",
                    sender,
                    reason,
                )

                return

            # ------------------------------------------------
            # CONTEXT ROUTER: решаем, какие куски контекста
            # (память/история/экономика/рынок/дружба с
            # russoturisto) реально нужны ЭТОМУ сообщению —
            # сначала бесплатный Python precheck, и только если
            # что-то осталось неясным, один дешёвый вызов
            # LLM-роутера строго под оставшееся (см. router.py).
            # Смотрим на remainder_text (без цитаты) по тем же
            # причинам, что и mention_russoturisto/is_direct_to_me
            # выше — собственные слова автора, а не чужая цитата.
            #
            # request_id — один короткий id на ВСЁ это сообщение,
            # общий для [РОУТЕР][id]/[MAIN][id] в логах (см. router.py/
            # llm.py) — чтобы расследовать "Пёс стал тупым" можно было
            # по одному id, а не сопоставлять записи по времени.
            request_id = uuid.uuid4().hex[:8]

            context_needs = await self.resolve_context_needs(
                remainder_text,
                mention_russoturisto,
                is_direct_to_me,
                target_nick,
                sender=sender,
                quoted_text=quoted_text,
                quote_author=quote_author,
                mentioned_nicks=mentioned_nicks,
                # Нужно ТОЛЬКО для Python-фолбэка archive_target_date
                # (see router.py:_resolve_router_temporal_date) — если
                # роутер решит needs_chat_archive=True, но не назовёт
                # (или не сможет назвать) дату, "вчера"/"сегодня" в
                # тексте/цитате считаются от этого момента, а не от
                # момента, когда LLM-роутер сам ответит.
                message_time=get_current_time(),
                request_id=request_id,
            )

            response = await self.ask_llm(
                sender,
                # Тег обращения "<ник>:" всегда снимается с текста
                # перед отправкой в LLM — это метаданные (кто адресат),
                # а не часть сообщения. Раньше тег снимался только при
                # обращении к самому боту, из-за чего в остальных
                # случаях модель видела "russoturisto: базу обнулил)"
                # как встроенную реплику от лица russoturisto.
                remainder_text,
                target_nick,
                mention_russoturisto,
                user_id=user_id,
                quoted_text=quoted_text,
                quote_author=quote_author,
                load_level=load_level,
                context_needs=context_needs,
                mentioned_nicks=mentioned_nicks,
                # Нужно ask_llm, чтобы отличить "меня реально
                # спросили" от "я случайно влез(ла) в чужой разговор"
                # (см. trigger ниже и random_butt_in_block в llm.py) —
                # без этого модель не знает, что 2-е лицо в тексте
                # может относиться к target_nick, а не к ней самой.
                is_direct_to_me=is_direct_to_me,
                request_id=request_id,
            )

            if not response:
                return

            await self.process_llm_response(
                sender,
                response,
                room_jid,
                user_id=user_id,
            )

        except Exception:

            logging.exception(
                "[ЧАТ] "
                "Неперехваченная ошибка "
                "обработки сообщения"
            )

    # ========================================================
    # LLM RESPONSE PROCESSING
    # ========================================================

    def sanitize_llm_response(
        self,
        response,
    ):
        if not isinstance(
            response,
            dict,
        ):
            return None

        body = response.get(
            "body",
            "Чё надо?",
        )

        if isinstance(
            body,
            dict,
        ):
            body = body.get(
                "body",
                "",
            )

        body = str(
            body
        ).strip()

        if not body:
            body = "Чё надо?"

        try:
            decision = int(
                response.get(
                    "decision",
                    0,
                )
            )
        except (
            ValueError,
            TypeError,
        ):
            decision = 0

        decision = (
            1
            if decision == 1
            else 0
        )

        try:
            rep_change = int(
                response.get(
                    "rep_change",
                    0,
                )
            )
        except (
            ValueError,
            TypeError,
        ):
            rep_change = 0

        rep_change = clamp(
            rep_change,
            -30,
            30,
        )

        facts = response.get(
            "facts",
            [],
        )

        facts = (
            self.db
            .sanitize_facts(
                facts
            )
        )

        # ----------------------------------------------------
        # GRANT COINS
        # ----------------------------------------------------

        try:
            grant = int(
                response.get(
                    "grant_coins",
                    0,
                )
            )
        except (
            ValueError,
            TypeError,
        ):
            grant = 0

        if (
            grant < 100
            or grant > 10000
        ):
            grant = 0

        # ----------------------------------------------------
        # CREATE LOT
        # ----------------------------------------------------

        lot = response.get(
            "create_lot"
        )

        clean_lot = None

        if isinstance(
            lot,
            dict,
        ):

            item = str(
                lot.get(
                    "item",
                    "",
                )
            ).strip()[:100]

            info = str(
                lot.get(
                    "full_info",
                    "",
                )
            ).strip()[:3000]

            try:
                price = float(
                    lot.get(
                        "base_price",
                        2000,
                    )
                )
            except (
                ValueError,
                TypeError,
            ):
                price = 0

            if (
                item
                and info
                and 100 <= price <= 1_000_000
                and not any(
                    marker in info.casefold()
                    for marker in SECRET_MARKERS
                )
                and not contains_high_entropy_secret(info)
            ):

                clean_lot = {
                    "item": item,
                    "full_info": info,
                    "base_price": price,
                }

        # ----------------------------------------------------
        # PUMP
        # ----------------------------------------------------

        pump_id = response.get(
            "pump_lot_id"
        )

        try:
            pump_id = (
                int(pump_id)
                if pump_id is not None
                else None
            )
        except (
            ValueError,
            TypeError,
        ):
            pump_id = None

        return {
            "body": body,
            "decision": decision,
            "rep_change": rep_change,
            "facts": facts,
            "grant_coins": grant,
            "create_lot": clean_lot,
            "pump_lot_id": pump_id,
        }

    async def process_llm_response(
        self,
        sender,
        response,
        room_jid,
        user_id=None,
    ):
        response = (
            self.sanitize_llm_response(
                response
            )
        )

        if not response:
            return

        body = response[
            "body"
        ]

        # ----------------------------------------------------
        # ACCOUNT-ONLY: репутация и личные факты требуют настоящего
        # user_id. Раньше здесь был account_id = user_id or sender —
        # для незарегистрированного собеседника это тихо завело бы
        # запись в users с nickname вместо user_id в качестве ключа.
        # Теперь, когда process_llm_response вызывается и для
        # незарегистрированных, оба блока просто пропускаются, если
        # аккаунта нет.
        # ----------------------------------------------------

        if user_id:

            # REPUTATION
            if response[
                "rep_change"
            ]:

                new_rep = (
                    await self.db
                    .add_reputation(
                        user_id,
                        response[
                            "rep_change"
                        ],
                    )
                )

                logging.info(
                    "[РЕПУТАЦИЯ] %s: %+d -> %d",
                    sender,
                    response["rep_change"],
                    new_rep,
                )

            # FACTS
            if response["facts"]:

                await self.db.add_facts(
                    user_id,
                    response["facts"],
                )

        # ----------------------------------------------------
        # LOT
        # ----------------------------------------------------

        lot = response[
            "create_lot"
        ]

        if lot:

            lot_id = (
                await self.db.create_lot(
                    lot["item"],
                    lot["full_info"],
                    lot["base_price"],
                )
            )

            if lot_id:

                body += (
                    "\n\n🔔 Пёс почуял "
                    "ценную инфу. "
                    f"На теневом рынке "
                    f"появился лот #{lot_id}: "
                    f"«{lot['item']}»."
                )

        # ----------------------------------------------------
        # PUMP
        # ----------------------------------------------------

        pump_id = response[
            "pump_lot_id"
        ]

        if pump_id:

            if await self.db.pump_lot(
                pump_id
            ):

                body += (
                    f"\n\n🔥 Интерес к лоту "
                    f"#{pump_id} подогрет."
                )

        # ----------------------------------------------------
        # GRANT (тоже требует настоящего account — игровая валюта
        # незарегистрированным не начисляется)
        # ----------------------------------------------------

        grant = response[
            "grant_coins"
        ]

        if grant and user_id:

            await self.db.add_coins(
                user_id,
                grant,
                transaction_type="LLM_REWARD",
                description=(
                    "Бонус от Пса за сообщение"
                ),
            )

            body += (
                f"\n\n💵 Пёс расщедрился: "
                f"+{grant} пёс_коинов."
            )

        # ----------------------------------------------------
        # SEND
        # ----------------------------------------------------

        self._send_dog_message(
            room_jid,
            body,
        )

        self.engaged = (
            response[
                "decision"
            ]
            == 1
        )

    # ========================================================
    # OPENROUTER
    # ========================================================

