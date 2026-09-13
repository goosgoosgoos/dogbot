from .config import *

class DogCommandsMixin:
    
    def get_wild_hunt_help(self, is_admin=False):
        text = (
            "🏴‍☠️ **ДИКАЯ ОХОТА**\n\n"
            "⚓ Морская охота на вражеские корабли. "
            "Пёс ведёт судно, а вы становитесь экипажем корабля.\n\n"
            "**👥 Участие**\n"
            "• Зарегистрируйтесь в комнате и присоединяйтесь к Охоте.\n"
            "• Получите роль в экипаже.\n"
            "• Приказы можно отдавать обычным языком.\n\n"
            "**⚔️ В бою**\n"
            "• Стрельба из пушек\n"
            "• Перезарядка орудий\n"
            "• Манёвры и изменение дистанции\n"
            "• Сближение с противником\n"
            "• Отход и уклонение\n"
            "• Абордаж\n"
            "• Ремонт повреждений\n"
            "• Управление экипажем\n\n"
            "**💬 Примеры приказов**\n"
            "`!охота стреляем левым бортом`\n"
            "`!охота огонь по вражескому кораблю`\n"
            "`!охота заряжаем пушки`\n"
            "`!охота сближаемся с врагом`\n"
            "`!охота разворачиваемся`\n"
            "`!охота отходим`\n"
            "`!охота готовимся к абордажу`\n"
            "`!охота идём на абордаж`\n"
            "`!охота ремонтируем корабль`\n\n"
            "**🧠 Важно**\n"
            "Не обязательно знать технические команды. "
            "Пёс понимает обычные приказы и переводит их "
            "в действия боевой системы.\n\n"
            "**☠️ Что может произойти**\n"
            "Вражеские корабли действуют самостоятельно. "
            "У них есть капитаны, экипажи, оружие и собственная тактика.\n"
            "Корабль можно повредить, лишить хода, взять на абордаж "
            "или потопить.\n\n"
            "🎯 Цель Охоты — найти и уничтожить либо захватить "
            "вражеские суда."
        )
        if is_admin:
            text += (
                "\n\n**👑 АДМИНИСТРАТОР**\n"
                "`!охота начать ЧЧ:ММ` — запланировать Охоту\n"
                "`!охота отмена` — отменить Охоту\n"
                "`!охота статус` — состояние события\n"
            )
        return text

    async def handle_commands(
        self,
        sender,
        text,
        room_jid,
    ):
        lower = (
            text.casefold()
            .strip()
        )

        inf = (
            await self.db
            .get_inflation_multiplier()
        )

        # ----------------------------------------------------
        # HELP
        # ----------------------------------------------------

        if lower == "!help":

            help_text = (
                "📜 КОМАНДЫ ПСА\n"
                "\n"
                "🔐 Сначала: !согласие, затем !регистрация "
                "(выдаст токен доступа).\n"
                "Дальше просто пиши команду в общий чат — Пёс "
                "спросит токен в личке при первом обращении.\n"
                "Пока ты онлайн в комнате — токен спрашивать не "
                "будет. Разорвалась сессия (диссконнект/выход) — "
                "спросит заново.\n"
                "\n"
                "!баланс — твой баланс\n"
                "!банк — банк, денежная масса "
                "и инфляция\n"
                "!мое досье — кличка, баланс, "
                "репутация и память\n"
                "!досье — то же самое\n"
                "!история — последние операции\n"
                "!рынок — активные инфо-лоты\n"
                "!мои инвестиции — твои доли\n"
                "!должность — показать свою должность\n"
                "!должность <ник> <должность> — назначить должность\n"
                "!снять_должность <ник> — снять должность\n"
                "!сменить_токен — выпустить новый токен взамен "
                "старого (нужен свежий ввод текущего токена)\n"
                "!отозвать — отозвать согласие на обработку "
                "данных (нужен свежий ввод токена)\n"
                "\n"
                "🏴‍☠️ ДИКАЯ ОХОТА\n"
                "!дикая_охота — полная справка по морской Охоте\n"
                "!вайтлист [добавить|убрать] <ник_или_jid> — "
                "вайтлист модерации (админ, доступ по JID)\n"
                "\n"
                "📈 РЫНОК\n"
                "!выкупить <номер_лота> — выкупить лот\n"
                "!инвест <номер_лота> <сумма> — инвестировать в лот\n"
                "!продать долю <номер_лота> — выйти полностью\n"
                "!продать долю <номер_лота> <сумма> — "
                "выйти частично\n"
                "\n"
                "💰 МАГАЗИН\n"
                "!купить ник_себе <новый ник>\n"
                '!купить ник_другому "<текущий>" "<новый>"\n'
                "!купить уважение_1\n"
                "!купить сказка\n"
                "!напоить — плеснуть Псу "
                "выпить (5 стадий до "
                "«в сопли»)\n"
            )

            self.send_message(
                mto=room_jid,
                mbody=help_text,
                mtype="chat",
            )

            return True

        # ----------------------------------------------------
        # WILD HUNT HELP COMMAND
        # ----------------------------------------------------

        if lower in ("!дикая_охота", "!дикая охота"):
            
            # Проверяем, является ли пользователь админом
            actor_nick, _ = await self.db.get_user_info(sender)
            is_admin = str(actor_nick or "").casefold() in BOT_ADMIN_NICKS
            
            help_msg = self.get_wild_hunt_help(is_admin=is_admin)

            self.send_message(
                mto=room_jid,
                mbody=help_msg,
                mtype="chat",
            )
            
            return True


        # ----------------------------------------------------
        # SHIP POSITION / CREW HIERARCHY
        # ----------------------------------------------------

        if lower == "!должность":
            position = await self.db.get_ship_position(sender)
            self.send_message(
                mto=room_jid,
                mbody=f"⚓ Твоя должность: {position or 'не назначена'}.",
                mtype="chat",
            )
            return True

        if lower.startswith("!должность "):
            parts = text.strip().split(None, 2)
            if len(parts) < 3:
                self.send_message(mto=room_jid, mbody="Формат: !должность <ник> <должность>", mtype="chat")
                return True
            target_nick, raw_position = parts[1], parts[2].strip()
            position = SHIP_POSITION_ALIASES.get(raw_position.casefold())
            if not position:
                available = ", ".join(SHIP_POSITIONS.keys())
                self.send_message(mto=room_jid, mbody=f"❌ Неизвестная должность. Доступны: {available}", mtype="chat")
                return True
            actor_position = await self.db.get_ship_position(sender)
            actor_level = SHIP_POSITIONS.get(actor_position, {}).get("level", 0)
            target = await self.db.get_account_by_nick(target_nick)
            if not target:
                self.send_message(mto=room_jid, mbody=f"❌ «{target_nick}» не зарегистрирован.", mtype="chat")
                return True
            target_position = await self.db.get_ship_position(target["user_id"])
            target_level = SHIP_POSITIONS.get(target_position, {}).get("level", 0)
            new_level = SHIP_POSITIONS[position]["level"]
            # Админы могут управлять всей иерархией; остальные — только
            # назначать должности строго ниже своей и не трогать равных/старших.
            actor_nick, _ = await self.db.get_user_info(sender)
            is_admin = str(actor_nick or "").casefold() in BOT_ADMIN_NICKS
            if not is_admin and actor_level <= 0:
                self.send_message(mto=room_jid, mbody="❌ У тебя нет командной должности для назначения экипажа.", mtype="chat")
                return True
            if not is_admin and (target_level >= actor_level or new_level >= actor_level):
                self.send_message(mto=room_jid, mbody="❌ По иерархии ты не можешь назначить эту должность этому члену экипажа.", mtype="chat")
                return True
            await self.db.set_ship_position(target["user_id"], position)
            self.send_message(mto=room_jid, mbody=f"⚓ {target_nick} получает должность: {position}.", mtype="chat")
            return True

        if lower.startswith("!снять_должность"):
            parts = text.strip().split(None, 1)
            if len(parts) < 2:
                self.send_message(mto=room_jid, mbody="Формат: !снять_должность <ник>", mtype="chat")
                return True
            target_nick = parts[1].strip()
            target = await self.db.get_account_by_nick(target_nick)
            if not target:
                self.send_message(mto=room_jid, mbody=f"❌ «{target_nick}» не зарегистрирован.", mtype="chat")
                return True
            actor_position = await self.db.get_ship_position(sender)
            actor_level = SHIP_POSITIONS.get(actor_position, {}).get("level", 0)
            target_position = await self.db.get_ship_position(target["user_id"])
            target_level = SHIP_POSITIONS.get(target_position, {}).get("level", 0)
            actor_nick, _ = await self.db.get_user_info(sender)
            is_admin = str(actor_nick or "").casefold() in BOT_ADMIN_NICKS
            if not is_admin and (actor_level <= 0 or target_level >= actor_level):
                self.send_message(mto=room_jid, mbody="❌ По иерархии ты не можешь снять должность с этого члена экипажа.", mtype="chat")
                return True
            await self.db.set_ship_position(target["user_id"], None)
            self.send_message(mto=room_jid, mbody=f"⚓ Должность {target_nick} снята.", mtype="chat")
            return True

        # ----------------------------------------------------
        # CHANGE TOKEN
        # ----------------------------------------------------

        if lower == "!сменить_токен":

            new_secret = await self.db.change_password(
                sender
            )

            if not new_secret:

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "❌ Не удалось выпустить новый "
                        "токен. Аккаунт не найден."
                    ),
                    mtype="chat",
                )

                return True

            new_token = f"{sender}:{new_secret}"

            self.send_message(
                mto=room_jid,
                mbody=(
                    "✅ Токен обновлён.\n"
                    f"Новый токен:\n{new_token}\n\n"
                    "Сохрани его — старый токен больше "
                    "не действует."
                ),
                mtype="chat",
            )

            return True

        # ----------------------------------------------------
        # REVOKE CONSENT
        # ----------------------------------------------------

        if lower == "!отозвать":

            nick_part = (
                room_jid.split("/", 1)[1]
                if "/" in room_jid
                else None
            )

            if nick_part:
                self.clear_session(nick_part)

            await self.revoke_consent(
                sender,
                room_jid,
            )

            return True

        # ----------------------------------------------------
        # WILD HUNT
        # ----------------------------------------------------

        if lower == "!охота" or lower.startswith("!охота "):

            await self.handle_hunt_command(
                sender,
                text,
                room_jid,
            )

            return True

        # ----------------------------------------------------
        # МОДЕРАЦИЯ: вайтлист по bare JID (доступ — MODERATION_
        # WHITELIST_ADMIN_JIDS, проверяется внутри обработчика)
        # ----------------------------------------------------

        if lower == "!вайтлист" or lower.startswith("!вайтлист "):

            await self.handle_moderation_whitelist_command(
                sender,
                text,
                room_jid,
            )

            return True

        # ----------------------------------------------------
        # BALANCE
        # ----------------------------------------------------

        if lower == "!баланс":

            balance = (
                await self.db.get_balance(
                    sender
                )
            )

            self.send_message(
                mto=room_jid,
                mbody=(
                    f"💰 Твой баланс: "
                    f"{balance:.2f} пёс_коинов."
                ),
                mtype="chat",
            )

            return True

        # ----------------------------------------------------
        # BANK
        # ----------------------------------------------------

        if lower == "!банк":

            bank = (
                await self.db
                .get_bank_balance()
            )

            money_supply = (
                await self.db
                .get_money_supply()
            )

            self.send_message(
                mto=room_jid,
                mbody=(
                    "🏦 ЦЕНТРОБАНК ПСА\n"
                    f"Резерв: {bank:.2f} коинов\n"
                    f"Денежная масса: "
                    f"{money_supply:.2f}\n"
                    f"Инфляция: x{inf:.4f}\n"
                    f"Диапазон инфляции: "
                    f"x{MIN_INFLATION:.1f}–"
                    f"x{MAX_INFLATION:.1f}"
                ),
                mtype="chat",
            )

            return True

        # ----------------------------------------------------
        # DOSSIER
        # ----------------------------------------------------

        if lower in (
            "!мое досье",
            "!досье",
            "!кто я",
        ):

            dossier = (
                await self.db
                .get_user_dossier(
                    sender
                )
            )

            msg = (
                f"📋 ДОСЬЕ: {sender}\n"
                f"Кличка: "
                f"{dossier['nickname'] or 'Отсутствует'}\n"
                f"Баланс: "
                f"{dossier['balance']:.2f} коинов\n"
                f"Репутация: "
                f"{dossier['reputation']} "
                f"({dossier['title']})\n"
                f"Должность: {dossier.get('ship_position') or 'не назначена'}\n"
            )

            if dossier[
                "respect_active"
            ]:
                msg += (
                    "🤵 Купленное уважение: активно\n"
                )

            facts = dossier[
                "facts"
            ]

            if facts:

                msg += (
                    "\n🧠 Пёс помнит:\n"
                )

                for i, fact in enumerate(
                    facts,
                    1,
                ):

                    msg += (
                        f"{i}. "
                        f"{fact.get('fact', '')} "
                        f"["
                        f"{fact.get('category', 'INFO')}, "
                        f"{fact.get('importance', 5)}/10"
                        f"{', граница' if fact.get('negative') else ''}"
                        f"]\n"
                    )

            else:
                msg += (
                    "\n🧠 Пёс пока ничего "
                    "полезного не запомнил."
                )

            self.send_message(
                mto=room_jid,
                mbody=msg,
                mtype="chat",
            )

            return True

        # ----------------------------------------------------
        # HISTORY
        # ----------------------------------------------------

        if lower == "!история":

            transactions = (
                await self.db
                .get_transactions(
                    sender,
                    15,
                )
            )

            if not transactions:

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "📜 История пуста."
                    ),
                    mtype="chat",
                )

                return True

            msg = (
                "📜 ПОСЛЕДНИЕ ОПЕРАЦИИ\n"
            )

            for tx in transactions:

                amount = float(
                    tx["amount"]
                )

                if amount > 0:
                    sign = "+"
                else:
                    sign = ""

                description = (
                    tx["description"]
                    or tx["type"]
                )

                created = str(
                    tx["created_at"]
                )[:19]

                msg += (
                    f"{created} | "
                    f"{sign}{amount:.2f} | "
                    f"{description}\n"
                )

            self.send_message(
                mto=room_jid,
                mbody=msg,
                mtype="chat",
            )

            return True

        # ----------------------------------------------------
        # MY INVESTMENTS
        # ----------------------------------------------------

        if lower in (
            "!мои инвестиции",
            "!инвестиции",
        ):

            investments = (
                await self.db
                .get_user_investments(
                    sender
                )
            )

            if not investments:

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "📉 Активных инвестиций нет."
                    ),
                    mtype="chat",
                )

                return True

            msg = (
                "📊 ТВОИ ИНВЕСТИЦИИ\n"
            )

            for inv in investments:

                msg += (
                    f"🔹 Лот #{inv['lot_id']} "
                    f"«{inv['item_name']}» — "
                    f"{float(inv['amount']):.2f} "
                    f"коинов\n"
                )

            msg += (
                "\n"
                "!продать долю <ID> — "
                "продать всё\n"
                "!продать долю <номер_лота> <сумма> — "
                "продать часть"
            )

            self.send_message(
                mto=room_jid,
                mbody=msg,
                mtype="chat",
            )

            return True

        # ----------------------------------------------------
        # MARKET
        # ----------------------------------------------------

        if lower == "!рынок":

            lots = (
                await self.db
                .get_active_lots()
            )

            if not lots:

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "📉 Рынок пуст. "
                        "Никаких грязных секретов "
                        "в продаже."
                    ),
                    mtype="chat",
                )

                return True

            msg = (
                "📈 ТЕНЕВОЙ РЫНОК СЕКРЕТОВ\n"
            )

            for lot in lots:

                current = round(
                    float(
                        lot["base_price"]
                    )
                    * float(
                        lot["multiplier"]
                    )
                    * inf,
                    2,
                )

                msg += (
                    f"🔹 #{lot['id']} "
                    f"{lot['item_name']} | "
                    f"{current:.2f} коинов | "
                    f"хайп x"
                    f"{float(lot['multiplier']):.2f}\n"
                )

            msg += (
                "\n"
                "!выкупить <ID> — купить полностью\n"
                "!инвест <ID> <сумма> — вложиться\n"
                "!мои инвестиции — мои доли"
            )

            self.send_message(
                mto=room_jid,
                mbody=msg,
                mtype="chat",
            )

            return True

        # ----------------------------------------------------
        # INVEST
        # ----------------------------------------------------

        if lower.startswith(
            "!инвест"
        ):

            args = (
                self.parse_quoted_args(
                    text
                )
            )

            if len(args) != 3:

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "Формат: "
                        "!инвест <ID> <сумма>"
                    ),
                    mtype="chat",
                )

                return True

            try:
                lot_id = int(
                    args[1]
                )

                amount = float(
                    args[2]
                )

            except ValueError:

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "ID и сумма должны "
                        "быть числами."
                    ),
                    mtype="chat",
                )

                return True

            success, result = (
                await self.db
                .invest_in_lot(
                    sender,
                    lot_id,
                    amount,
                )
            )

            self.send_message(
                mto=room_jid,
                mbody=result,
                mtype="chat",
            )

            return True

        # ----------------------------------------------------
        # SELL INVESTMENT
        # ----------------------------------------------------

        if lower.startswith(
            "!продать долю"
        ):

            args = (
                self.parse_quoted_args(
                    text
                )
            )

            if len(args) not in (
                3,
                4,
            ):

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "Формат:\n"
                        "!продать долю <ID>\n"
                        "или\n"
                        "!продать долю <ID> <сумма>"
                    ),
                    mtype="chat",
                )

                return True

            try:
                lot_id = int(
                    args[2]
                )
            except ValueError:

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "ID лота должен "
                        "быть числом."
                    ),
                    mtype="chat",
                )

                return True

            amount = None

            if len(args) == 4:
                try:
                    amount = float(
                        args[3]
                    )
                except ValueError:

                    self.send_message(
                        mto=room_jid,
                        mbody=(
                            "Сумма должна "
                            "быть числом."
                        ),
                        mtype="chat",
                    )

                    return True

            success, result = (
                await self.db
                .sell_investment(
                    sender,
                    lot_id,
                    amount,
                )
            )

            self.send_message(
                mto=room_jid,
                mbody=result,
                mtype="chat",
            )

            return True

        # ----------------------------------------------------
        # BUYOUT
        # ----------------------------------------------------

        if lower.startswith(
            "!выкупить"
        ):

            args = (
                self.parse_quoted_args(
                    text
                )
            )

            if len(args) != 2:

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "Формат: "
                        "!выкупить <ID>"
                    ),
                    mtype="chat",
                )

                return True

            try:
                lot_id = int(
                    args[1]
                )
            except ValueError:

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "ID лота должен "
                        "быть числом."
                    ),
                    mtype="chat",
                )

                return True

            success, data = (
                await self.db
                .buyout_lot(
                    sender,
                    lot_id,
                )
            )

            if not success:

                self.send_message(
                    mto=room_jid,
                    mbody=data,
                    mtype="chat",
                )

                return True

            msg = (
                f"🔓 {sender} выкупил лот "
                f"«{data['item']}» "
                f"за {data['price']:.2f} коинов!\n\n"
                f"РАССЕКРЕЧЕНО:\n"
                f"{data['info']}"
            )

            if data["payouts"]:

                msg += (
                    "\n\n💸 ВЫПЛАТЫ ИНВЕСТОРАМ:\n"
                    + "\n".join(
                        data["payouts"]
                    )
                )

            self.send_message(
                mto=room_jid,
                mbody=msg,
                mtype="chat",
            )

            return True

        # ----------------------------------------------------
        # НАПОИТЬ ПСА
        # ----------------------------------------------------

        if lower == "!напоить":

            current_level = (
                await self.db
                .get_drunk_level()
            )

            if current_level >= MAX_DRUNK_LEVEL:

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "🐶💤 Пёс уже "
                        "нажрался в сопли. "
                        "Дальше уже некуда, "
                        "пусть проспится."
                    ),
                    mtype="chat",
                )

                return True

            cost = round(
                DRUNK_STAGE_COST * inf,
                2,
            )

            success, balance = (
                await self.db
                .charge_user_and_credit_bank(
                    sender,
                    cost,
                    "SHOP_MAKE_DRUNK",
                    "Налил Псу выпить",
                )
            )

            if not success:

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        f"Нужно {cost:.2f}. "
                        f"У тебя {balance:.2f}."
                    ),
                    mtype="chat",
                )

                return True

            new_level = (
                await self.db
                .add_drunk_level(1)
            )

            flavor = (
                DRUNK_STAGE_REPLIES.get(
                    new_level,
                    "Пёс опьянел ещё "
                    "сильнее.",
                )
            )

            msg = (
                f"🍺 {sender} наливает "
                f"Псу.\n"
                f"{flavor}\n"
                f"Стадия опьянения: "
                f"{new_level}/"
                f"{MAX_DRUNK_LEVEL}.\n"
                f"Списано {cost:.2f} "
                f"коинов."
            )

            self.send_message(
                mto=room_jid,
                mbody=msg,
                mtype="chat",
            )

            return True

        # ----------------------------------------------------
        # SHOP
        # ----------------------------------------------------

        if lower.startswith(
            "!купить"
        ):

            args = (
                self.parse_quoted_args(
                    text
                )
            )

            if len(args) < 2:

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "Укажи товар. "
                        "Список: !help"
                    ),
                    mtype="chat",
                )

                return True

            item = (
                args[1]
                .casefold()
            )

            # ------------------------------------------------
            # RESPECT
            # ------------------------------------------------

            if item == "уважение_1":

                cost = round(
                    15000 * inf,
                    2,
                )

                success, balance = (
                    await self.db
                    .charge_user_and_credit_bank(
                        sender,
                        cost,
                        "SHOP_RESPECT",
                        "Покупка уважения на 24 часа",
                    )
                )

                if not success:

                    self.send_message(
                        mto=room_jid,
                        mbody=(
                            f"Нужно {cost:.2f}. "
                            f"У тебя {balance:.2f}."
                        ),
                        mtype="chat",
                    )

                    return True

                new_rep = (
                    await self.db
                    .add_reputation(
                        sender,
                        100,
                    )
                )

                await self.db.set_respect(
                    sender,
                    1,
                )

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        f"🤵 Сделка заключена. "
                        f"Репутация: {new_rep}. "
                        "24 часа Пёс обязан "
                        "соблюдать уважительный режим."
                    ),
                    mtype="chat",
                )

                return True

            # ------------------------------------------------
            # NICK SELF
            # ------------------------------------------------

            if item == "ник_себе":

                if len(args) < 3:

                    self.send_message(
                        mto=room_jid,
                        mbody=(
                            "Формат: "
                            "!купить ник_себе "
                            "<новый ник>"
                        ),
                        mtype="chat",
                    )

                    return True

                new_nick = (
                    " ".join(
                        args[2:]
                    ).strip()
                )

                if (
                    new_nick.casefold()
                    == self.nick.casefold()
                ):

                    self.send_message(
                        mto=room_jid,
                        mbody=(
                            "Нельзя использовать "
                            "кличку Пса."
                        ),
                        mtype="chat",
                    )

                    return True

                cost = round(
                    10000 * inf,
                    2,
                )

                success, balance = (
                    await self.db
                    .charge_user_and_credit_bank(
                        sender,
                        cost,
                        "SHOP_NICKNAME",
                        "Покупка собственной клички",
                    )
                )

                if not success:

                    self.send_message(
                        mto=room_jid,
                        mbody=(
                            f"Нужно {cost:.2f}. "
                            f"У тебя {balance:.2f}."
                        ),
                        mtype="chat",
                    )

                    return True

                await self.db.update_nickname(
                    sender,
                    new_nick,
                )

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        f"Готово. Теперь твоя "
                        f"кличка «{new_nick}».\n"
                        f"Баланс: {balance:.2f}."
                    ),
                    mtype="chat",
                )

                return True

            # ------------------------------------------------
            # NICK OTHER
            # ------------------------------------------------

            if item == "ник_другому":

                if len(args) != 4:

                    self.send_message(
                        mto=room_jid,
                        mbody=(
                            'Формат: '
                            '!купить ник_другому '
                            '"<текущий>" "<новый>"'
                        ),
                        mtype="chat",
                    )

                    return True

                target = args[2].strip()

                new_nick = args[3].strip()

                if (
                    new_nick.casefold()
                    == self.nick.casefold()
                ):

                    self.send_message(
                        mto=room_jid,
                        mbody=(
                            "Нельзя выдать "
                            "другому мою кличку."
                        ),
                        mtype="chat",
                    )

                    return True

                cursor = (
                    await self.db.conn.execute(
                        """
                        SELECT user_id
                        FROM users
                        WHERE nickname = ?
                           OR user_id = ?
                        LIMIT 1
                        """,
                        (
                            target,
                            target,
                        ),
                    )
                )

                row = (
                    await cursor.fetchone()
                )

                if not row:

                    self.send_message(
                        mto=room_jid,
                        mbody=(
                            "Не нашёл такого "
                            "пользователя. "
                            "Деньги не списаны."
                        ),
                        mtype="chat",
                    )

                    return True

                cost = round(
                    50000 * inf,
                    2,
                )

                success, balance = (
                    await self.db
                    .charge_user_and_credit_bank(
                        sender,
                        cost,
                        "SHOP_OTHER_NICKNAME",
                        f"Покупка клички для {target}",
                        related_user=row["user_id"],
                    )
                )

                if not success:

                    self.send_message(
                        mto=room_jid,
                        mbody=(
                            f"Нужно {cost:.2f}. "
                            f"У тебя {balance:.2f}."
                        ),
                        mtype="chat",
                    )

                    return True

                await self.db.update_nickname(
                    row["user_id"],
                    new_nick,
                )

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        f"Готово. {target} "
                        f"теперь «{new_nick}».\n"
                        f"Списано {cost:.2f} коинов."
                    ),
                    mtype="chat",
                )

                return True

            # ------------------------------------------------
            # FAIRY TALE
            # ------------------------------------------------

            if item == "сказка":

                # Не позволяем одному пользователю
                # одновременно заказать бесконечное
                # количество сказок.
                allowed, reason = (
                    await self.llm_rate_limiter.allow(
                        sender
                    )
                )

                if not allowed:

                    self.send_message(
                        mto=room_jid,
                        mbody=reason,
                        mtype="chat",
                    )

                    return True

                cost = round(
                    6000 * inf,
                    2,
                )

                success, balance = (
                    await self.db
                    .charge_user_and_credit_bank(
                        sender,
                        cost,
                        "SHOP_FAIRY_TALE",
                        "Заказ сказки",
                    )
                )

                if not success:

                    self.send_message(
                        mto=room_jid,
                        mbody=(
                            "Сказки только "
                            "для богатых.\n"
                            f"Нужно {cost:.2f}, "
                            f"у тебя {balance:.2f}."
                        ),
                        mtype="chat",
                    )

                    return True

                self.send_message(
                    mto=room_jid,
                    mbody=(
                        "📖 Заказ принят. "
                        "Пёс пошёл сочинять..."
                    ),
                    mtype="chat",
                )

                asyncio.create_task(
                    self.tell_fairy_tale(
                        sender,
                        room_jid,
                        cost,
                    )
                )

                return True

            self.send_message(
                mto=room_jid,
                mbody=(
                    "Неизвестный товар. "
                    "Список: !help"
                ),
                mtype="chat",
            )

            return True

        return False

    # ========================================================
    # FAIRY TALE
    # ========================================================

    async def tell_fairy_tale(
        self,
        sender,
        room_jid,
        cost,
    ):
        try:

            prompt = (
                "Ты — Пёс, грубый и абсурдный рассказчик. "
                f"Напиши смешную циничную сказку на ночь "
                f"для {sender}. "
                "Вплети физику, квантовую механику "
                "и абсурд. "
                "3-4 абзаца. "
                "Только текст сказки."
            )

            result = await self.call_openrouter(
                [
                    {
                        "role": "system",
                        "content": (
                            "Пиши только саму сказку, "
                            "без JSON."
                        ),
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                json_mode=False,
                timeout=30,
                rate_limit=False,
            )

            if not result:
                raise RuntimeError(
                    "Пустой ответ LLM"
                )

            self.send_message(
                mto=room_jid,
                mbody=result,
                mtype="chat",
            )

        except Exception:

            logging.exception(
                "[СКАЗКА] Ошибка генерации"
            )

            await self.db.add_coins(
                sender,
                cost,
                transaction_type="REFUND",
                description=(
                    "Возврат за неудачную генерацию сказки"
                ),
            )

            self.send_message(
                mto=room_jid,
                mbody=(
                    "💸 Пёс не смог выполнить заказ. "
                    f"Вернул тебе {cost:.2f} коинов."
                ),
                mtype="chat",
            )

    # ========================================================
    # GROUP CHAT
    # ========================================================

