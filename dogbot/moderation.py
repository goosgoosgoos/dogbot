from collections import defaultdict, deque
from datetime import datetime
import logging
import re
import time
from slixmpp import JID

from .config import (
    MODERATION_ENABLED,
    MODERATION_SCORE_WATCH,
    MODERATION_SCORE_RESTRICT,
    MODERATION_SCORE_DELETE,
    MODERATION_SCORE_BAN,
    MODERATION_BAN_SUSTAINED_EVENTS,
    MODERATION_BAN_IDENTITY_SIGNALS,
    MODERATION_RESTRICT_SECONDS,
    MODERATION_RESTRICT_STRIKES_TO_ESCALATE,
    MODERATION_RESTRICT_STRIKE_WINDOW_SECONDS,
    MODERATION_TENURE_TRUSTED_MESSAGES,
    MODERATION_TENURE_TRUSTED_DAYS,
    MODERATION_TENURE_DAMPEN,
    MODERATION_WINDOW_SECONDS,
    MODERATION_CONTENT_WINDOW_SECONDS,
    MODERATION_LLM_ENABLED,
    MODERATION_LLM_EVERY_MESSAGES,
    MODERATION_LLM_COOLDOWN_SECONDS,
    MODERATION_LLM_MIN_MESSAGES,
    MODERATION_LLM_CONTEXT_MESSAGES,
    MODERATION_LLM_CONTEXT_WINDOW_SECONDS,
    MODERATION_LLM_GREYZONE_MIN,
    MODERATION_LLM_BOT_CONFIDENCE,
    MODERATION_LLM_WATCH_CONFIDENCE,
    MODERATION_WHITELIST_ADMIN_JIDS,
)


class DogModerationMixin:
    """
    Анти-рейд/анти-бот модерация MUC.

    Важный принцип:
      * решение о модерации принимается только если Пёс сам является
        moderator/admin/owner в MUC;
      * идентичность участника берётся из MUC occupant metadata
        (get_jid_property(..., "jid")), а не из msg["from"], потому что
        msg["from"] в MUC обычно содержит room/nick;
      * удаление чужих сообщений делается через XEP-0425 по MUC
        stanza-id (XEP-0359). Если сервер не дал stanza-id, безопасно
        "удалить" сообщение невозможно — оно только логируется;
      * решение о реакции принимается по ЕДИНОЙ числовой шкале score
        (см. _mod_score/_mod_level) — детерминированные эвристики
        считают баллы по многим независимым осям (частота, повтор/
        похожесть текста, паттерн интервалов, идентичность ника,
        доля в потоке комнаты, стаж в архиве комнаты), а score решает
        уровень реакции: ничего / подозрение / временное ограничение /
        удаление / бан. LLM (_mod_llm_check/classify_possible_bot в
        llm.py) вызывается ТОЛЬКО как арбитр в серой зоне score или
        когда эвристики между собой противоречат/не уверены — сама
        LLM ничего не решает, только классифицирует; финальное
        решение всегда принимает код через пороги в config.py.
    """

    MOD_BOT_HINT_RE = re.compile(
        r"(?:^|[\W_])(?:bot|бот|bridge|relay|service|служба|feed|"
        r"crawler|spammer|spam|raid|raider|ai|gpt|llm)(?:$|[\W_])",
        re.IGNORECASE | re.UNICODE,
    )

    # Убирает любую пунктуацию/эмодзи (не \w и не пробел) — рейдер,
    # который "обходит" точное совпадение простой сменой пунктуации
    # или капса ("СПАМ!!!" vs "спам", "спам..." vs "спам"), под
    # нормализацию больше не проходит незамеченным.
    _MOD_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
    # Схлопывает любые растянутые повторы символа длиной >=3
    # ("путинnnnn" -> "путин", "!!!!!" -> уже вырезано выше, но и
    # "ааааа" -> "а") — ещё одна дешёвая, но частая обфускация флуда.
    _MOD_REPEAT_CHAR_RE = re.compile(r"(.)\1{2,}", re.UNICODE)

    # Сигналы, которые в сумме считаются "поведением бота" для
    # структурного гейта постоянного бана вместе с bot_identity_hint —
    # см. _mod_ban_eligible.
    MOD_BOT_SIGNAL_REASONS = {
        "duplicate_flood",
        "near_duplicate_flood",
        "char_obfuscation_flood",
        "repeated_phrase_hint",
        "low_vocabulary",
        "machine_pacing",
        "pacing_evasion",
        "burst_5s",
        "burst_15s",
        "burst_30s",
        "link_flood",
        "room_share_dominant",
        "llm_bot_suspect",
    }

    def moderation_init(self):
        self.mod_events = defaultdict(lambda: deque(maxlen=80))
        # room_jid.casefold() -> deque(time.monotonic() всех сообщений
        # комнаты, включая уже зафиксированных ботов) — используется
        # только чтобы посчитать долю конкретного участника в общем
        # потоке комнаты (room_share_*, см. _mod_score). Отдельная
        # от per-user mod_events структура: здесь не нужен текст,
        # только временные метки, поэтому она намного легче.
        self.mod_room_events = defaultdict(lambda: deque(maxlen=400))
        self.mod_fixed_jids = set()
        self.mod_fixed_nicks = set()
        # Вайтлист (!вайтлист) — bare JID, освобождённые от анти-
        # бот/анти-флуд модерации; управляется отдельным JID-
        # allowlist'ом MODERATION_WHITELIST_ADMIN_JIDS (config.py),
        # НЕ пересекается с mod_fixed_jids/nicks (те — наоборот,
        # закреплённые боты). По умолчанию пусто здесь: реальные
        # записи подтягиваются из БД в core.py::start() при
        # подключении (moderation_init вызывается раньше, чем БД
        # готова, — см. core.py).
        self.mod_whitelist_jids = set()
        self.mod_warned_humans = set()
        # key -> time.monotonic() дедлайна "чувствительного окна"
        # после RESTRICT-уровня (см. moderate_incoming_muc и
        # _mod_llm_check). НЕ используется для тихого массового
        # удаления будущих сообщений — только чтобы вызывать
        # LLM-арбитра быстрее обычного кадансирования, пока участник
        # "под приглядом".
        self.mod_restricted_until = {}
        # key -> {"count": int, "last_at": monotonic} — счётчик
        # повторных попаданий в restrict за последнее время, см.
        # "три предупреждения" в moderate_incoming_muc и
        # MODERATION_RESTRICT_STRIKES_TO_ESCALATE в config.py.
        self.mod_restrict_strikes = {}
        # key -> {"first_seen": iso str|None, "message_count": int} —
        # кэш результата db.get_room_sender_stats на весь процесс
        # (см. _mod_tenure). Стаж только растёт, а гонять DB-запрос
        # на каждое сообщение бессмысленно.
        self.mod_tenure_cache = {}
        # room_jid.casefold() -> time.monotonic() последнего
        # диагностического предупреждения "модерация неактивна в
        # этой комнате" — см. _mod_notice_inactive. Раньше это поле
        # заводилось (mod_last_notice), но нигде не использовалось —
        # оба ранних return в moderate_incoming_muc (MODERATION_ENABLED
        # и отсутствие прав) молчали совсем, без единого лога.
        self.mod_last_notice = {}
        # key -> {"checked_at": monotonic, "verdict": {...} | None} —
        # состояние LLM-проверки на ник, см. _mod_llm_check.
        self.mod_llm_state = {}
        # key -> сколько сообщений пришло с последней LLM-проверки.
        # СОЗНАТЕЛЬНО отдельный счётчик, а не "len(events) на момент
        # проверки" (так было раньше и в этом самом патче до фикса):
        # events — скользящее окно с ретеншеном
        # MODERATION_CONTENT_WINDOW_SECONDS/MODERATION_WINDOW_SECONDS,
        # и после любой паузы длиннее этого окна len(events) резко
        # ПАДАЕТ (старые события вычищаются). Кадансный счётчик,
        # завязанный на абсолютную длину events, из-за этого мог
        # уйти в отрицательные значения и застрять НАВСЕГДА — именно
        # так реальный медленный рейдер (паузы 10-30с, изредка больше
        # окна ретеншена) остался невидим для LLM-арбитра после
        # самой первой проверки. mod_llm_pending считает сообщения
        # независимо от того, что происходит с events.
        self.mod_llm_pending = defaultdict(int)

    @staticmethod
    def _mod_canonical_jid(jid):
        value = str(jid or "").strip()
        if "/" in value:
            value = value.split("/", 1)[0]
        return value.casefold() or None

    def _mod_key(self, jid, nick):
        return (
            self._mod_canonical_jid(jid) or "",
            str(nick or "").casefold(),
        )

    def _mod_normalize(self, text):
        value = str(text or "").strip().casefold()
        # Пунктуация/эмодзи и растянутые повторы символов убираются
        # ДО схлопывания пробелов — "СПАМ!!!" и "спам" должны давать
        # одинаковый norm, иначе duplicate_flood/repetition ловят
        # только самую наивную версию флуда (см. докстроку класса).
        value = self._MOD_PUNCT_RE.sub(" ", value)
        value = self._MOD_REPEAT_CHAR_RE.sub(r"\1", value)
        return re.sub(r"\s+", " ", value).strip()

    def _mod_compact(self, text):
        """
        Как _mod_normalize, но убирает ВООБЩЕ ВСЕ пробелы, а не только
        схлопывает их.

        Нужно для обфускации, которую словесная токенизация
        (_mod_token_similarity) в принципе не может поймать: рейдер
        растягивает слово пробелами по одной букве ("л и з н и т е
        а н у с") или обкладывает его случайным мусором ("тxоi9zзу...
        л и з н и т е    а н    у с ...CюRр..."). После разбиения по
        пробелам каждая буква — отдельный "токен", и Jaccard по словам
        показывает ноль пересечения с обычным "лизните анус", хотя
        содержательно это одно и то же сообщение. Убрав пробелы
        полностью, получаем плотную строку символов, где смысловое
        ядро остаётся неразрывной подстрокой — см. _mod_char_containment.
        """
        return re.sub(r"\s+", "", self._mod_normalize(text))

    def _mod_char_containment(self, reference_compact, candidate_compact, n=3):
        """
        Доля n-грамм БОЛЕЕ КОРОТКОГО из двух compact-сообщений,
        встречающихся в более длинном.

        Специально не симметричный Jaccard: рейдер обычно обкладывает
        смысловое ядро мусором с одной или обеих сторон ("тxоi9zзу...
        <ядро> ...CюRр..."), и мусор раздувает знаменатель у обычного
        Jaccard, из-за чего похожесть на короткий "чистый" вариант того
        же сообщения занижается. "Доля вхождения короткого в длинное"
        не страдает от этого: если 11-символьное ядро "лизнитеанус"
        целиком содержится в 40-символьной строке с мусором, это будет
        видно независимо от того, сколько мусора добавлено вокруг.
        """
        if len(reference_compact) < n or len(candidate_compact) < n:
            return (
                1.0
                if reference_compact and reference_compact == candidate_compact
                else 0.0
            )

        ref_grams = {
            reference_compact[i : i + n]
            for i in range(len(reference_compact) - n + 1)
        }
        cand_grams = {
            candidate_compact[i : i + n]
            for i in range(len(candidate_compact) - n + 1)
        }
        shorter, longer = (
            (ref_grams, cand_grams)
            if len(ref_grams) <= len(cand_grams)
            else (cand_grams, ref_grams)
        )
        if not shorter:
            return 0.0
        return len(shorter & longer) / len(shorter)

    def _mod_token_similarity(self, reference_norms, candidate_norm, min_tokens=3):
        """
        Доля сообщений в reference_norms, чьё пересечение токенов с
        candidate_norm (Jaccard по множеству слов) превышает порог.

        Ловит эвазию "вставить в шаблон одно-два случайных слова"
        (п.5 ТЗ: "схожесть сообщений между собой") — exact-duplicate
        ratio в _mod_score такую пару не увидит вообще, т.к. norm
        отличается, а этот показатель — увидит, если ядро сообщения
        (большинство токенов) осталось тем же.

        ВАЖНО: это ловит только перестановку/замену ЦЕЛЫХ слов. Для
        обфускации ВНУТРИ слова (разбитие по буквам пробелами, мусорные
        символы) этот метод бесполезен по построению (whitespace-split
        превращает каждую букву в отдельный "токен") — см.
        _mod_char_containment, который считается на compact-строке без
        пробелов вообще и покрывает именно этот случай.
        """
        cand_tokens = set(candidate_norm.split())
        if len(cand_tokens) < min_tokens:
            return 0.0

        hits = 0
        total = 0
        for norm in reference_norms:
            tokens = set(norm.split())
            if len(tokens) < min_tokens:
                continue
            total += 1
            union = cand_tokens | tokens
            if not union:
                continue
            if len(cand_tokens & tokens) / len(union) >= 0.6:
                hits += 1

        return hits / total if total else 0.0

    def _mod_occupant_jid(self, room_jid, nick):
        try:
            jid = self.plugin["xep_0045"].get_jid_property(
                room_jid, nick, "jid"
            )
            return str(jid) if jid else None
        except Exception:
            logging.debug(
                "[МОДЕРАЦИЯ] Не удалось получить JID occupant %s",
                nick,
                exc_info=True,
            )
            return None

    def _mod_is_privileged(self, room_jid):
        """
        Для XEP-0425 реальное право — role=moderator.
        affiliation=admin/owner тоже считаем достаточным, т.к. такие
        аккаунты обычно получают модераторские права в MUC.
        """
        try:
            xep = self.plugin["xep_0045"]
            role = str(
                xep.get_jid_property(room_jid, self.nick, "role") or ""
            ).casefold()
            affiliation = str(
                xep.get_jid_property(
                    room_jid, self.nick, "affiliation"
                ) or ""
            ).casefold()
            return (
                role == "moderator"
                or affiliation in {"admin", "owner"}
            )
        except Exception:
            logging.exception(
                "[МОДЕРАЦИЯ] Не удалось проверить права Пса"
            )
            return False

    def _mod_notice_inactive(self, room_jid, reason):
        """
        MODERATION_ENABLED=0, отсутствие прав модератора и
        MODERATION_LLM_ENABLED=0 — все три совершенно законных
        состояния, поэтому не пишутся на каждое сообщение. Но их
        полная тишина в логах неотличима от "модерация работает и
        молчит, потому что нечего ловить" — реальный кейс: рейд-бот
        пишет по кругу каждые 15с несколько минут, а в journalctl
        нет ни одной строки [МОДЕРАЦИЯ], потому что Псу просто не
        выдали role=moderator в этой комнате.

        Поэтому: одно предупреждение на (комнату, причину) не чаще
        раза в 10 минут — этого достаточно, чтобы при расследовании
        сразу увидеть причину, и недостаточно, чтобы засорять журнал.
        """
        now = time.monotonic()
        key = (str(room_jid or "").casefold(), reason)
        last = self.mod_last_notice.get(key)

        if last is not None and now - last < 600:
            return

        self.mod_last_notice[key] = now

        if reason == "disabled":
            logging.warning(
                "[МОДЕРАЦИЯ] Выключена глобально (MODERATION_ENABLED=0) "
                "— ни эвристики, ни LLM-проверка не запускаются ни в "
                "одной комнате, включая %s.",
                room_jid,
            )
        elif reason == "llm_disabled":
            logging.warning(
                "[МОДЕРАЦИЯ] LLM-проверка выключена "
                "(MODERATION_LLM_ENABLED=0) в %s — числовые эвристики "
                "по-прежнему активны, но медленный/семантический "
                "флуд (напр. паузы ~15с между сообщениями) они не "
                "ловят в принципе, см. комментарий над "
                "MODERATION_LLM_ENABLED в config.py.",
                room_jid,
            )
        else:
            logging.warning(
                "[МОДЕРАЦИЯ] Нет прав модератора Пса в %s — анти-рейд/"
                "анти-бот полностью неактивен в этой комнате (эвристики "
                "и LLM-проверка не запускаются вообще, сколько бы кто "
                "ни флудил). Нужно выдать Псу role=moderator (или "
                "affiliation admin/owner) именно в этой комнате.",
                room_jid,
            )

    def _mod_score(self, events, text, nick, occupant_jid, room_recent_count=None):
        """
        Считает баллы риска по многим НЕЗАВИСИМЫМ осям. Смысл именно
        в независимости осей: рейдеру, который обходит одну (скажем,
        рандомизирует интервалы), всё ещё приходится иметь дело с
        остальными (повтор/похожесть текста, словарь, доля в потоке
        комнаты, идентичность ника) — обойти все сразу гораздо дороже,
        чем одну простую эвристику.

        room_recent_count — общее число сообщений В КОМНАТЕ (не
        только этого автора) за последние 15с, см. вызывающий код
        (moderate_incoming_muc) и self.mod_room_events. None/малое
        значение — сигнал доли в потоке комнаты просто не считается
        (в тихой комнате процент от 1-2 сообщений ничего не значит).
        """
        now = time.monotonic()
        recent_5 = [e for e in events if now - e["t"] <= 5]
        recent_15 = [e for e in events if now - e["t"] <= 15]
        recent_30 = [e for e in events if now - e["t"] <= 30]

        score = 0
        reasons = []

        if len(recent_5) >= 5:
            score += 3
            reasons.append("burst_5s")
        elif len(recent_15) >= 8:
            score += 2
            reasons.append("burst_15s")

        if len(recent_30) >= 12:
            score += 2
            reasons.append("burst_30s")

        # Устойчивый поток (а не один короткий всплеск) — отдельный
        # балл поверх burst_*. Важно для структурного гейта бана
        # (_mod_ban_eligible опирается на len(events), а не только на
        # score), но полезен и как явный вклад в саму шкалу: score
        # должен расти с продолжительностью злоупотребления, а не
        # только с его пиковой интенсивностью.
        if len(events) >= MODERATION_BAN_SUSTAINED_EVENTS:
            score += 2
            reasons.append("sustained_flood")

        recent_content = [
            e for e in events if now - e["t"] <= MODERATION_CONTENT_WINDOW_SECONDS
        ]

        # --- БЫСТРЫЙ повтор в УЗКОМ окне (recent_15). Здесь подход
        # "какая ДОЛЯ окна — дубликат" оправдан: в тесном burst-окне
        # почти все сообщения и есть флуд по построению, доля хорошо
        # отражает интенсивность.
        norms_15 = [e["norm"] for e in recent_15 if e["norm"]]
        near_duplicate_triggered = False
        if len(norms_15) >= 5:
            unique = len(set(norms_15))
            duplicate_ratio = 1.0 - unique / len(norms_15)
            if duplicate_ratio >= 0.60:
                score += 3
                reasons.append("duplicate_flood")
            elif duplicate_ratio >= 0.40:
                score += 1
                reasons.append("repetition")
            else:
                # Точных повторов мало — проверяем эвазию "вставить
                # случайное слово в шаблон" через похожесть токенов
                # текущего сообщения на предыдущие в этом же окне.
                sim_ratio = self._mod_token_similarity(norms_15[:-1], norms_15[-1])
                if sim_ratio >= 0.5:
                    score += 2
                    reasons.append("near_duplicate_flood")
                    near_duplicate_triggered = True

            # Очень маленький словарь на всё окно (п.7 ТЗ) — ловит
            # шаблонный флуд, размазанный по небольшому пулу слов,
            # даже когда никакие два сообщения по отдельности не
            # похожи достаточно для duplicate/near_duplicate.
            all_tokens = [t for n in norms_15 for t in n.split()]
            if len(all_tokens) >= 10:
                ttr = len(set(all_tokens)) / len(all_tokens)
                if ttr <= 0.35:
                    score += 2
                    reasons.append("low_vocabulary")

        # --- МЕДЛЕННЫЙ повтор в ШИРОКОМ окне (recent_content, по
        # умолчанию = весь срок хранения events). Рейдер, который
        # специально выдерживает паузы длиннее burst-окна, почти
        # никогда не даёт recent_15 больше 1-2 сообщений — приходится
        # смотреть шире. НО: здесь принципиально считаем АБСОЛЮТНОЕ
        # число совпадений, а не долю — в широком окне вперемешку с
        # повторами почти всегда есть и другой, непохожий текст того
        # же автора (другие оскорбления/лозунги), и доля от него
        # размывается ровно тогда, когда сигнал нужнее всего (так
        # изначально и произошло на реальном инциденте: hits/total
        # тонул в разнородном наполнителе и сигнал не срабатывал).
        # "Текущее сообщение похоже хотя бы на 2 недавних" — сигнал
        # сам по себе не зависит от того, сколько ещё РАЗНОГО было
        # вокруг.
        norms_wide = [e["norm"] for e in recent_content if e["norm"]]
        weak_repeat_hint = False
        if not near_duplicate_triggered and len(norms_wide) >= 3:
            candidate_norm = norms_wide[-1]
            word_hits = sum(
                1
                for n in norms_wide[:-1]
                if n == candidate_norm
                or self._mod_token_similarity([n], candidate_norm) >= 0.6
            )
            if word_hits >= 2:
                score += 2
                reasons.append("near_duplicate_flood")
            elif word_hits == 1:
                # Только ОДНО похожее сообщение в широком окне — рано
                # для полноценного near_duplicate_flood (мог быть и
                # случайным совпадением темы разговора), но уже не
                # ничего — копим как более мягкий, комбинируемый сигнал.
                weak_repeat_hint = True

        # Обфускация ВНУТРИ слова (буквы через пробел, обкладывание
        # мусорными символами) — словесная токенизация (near_duplicate
        # выше) её принципиально не видит, т.к. каждая буква становится
        # отдельным "словом". Сравниваем по n-граммам на строке БЕЗ
        # пробелов вообще (см. _mod_compact/_mod_char_containment),
        # тоже абсолютным счётом по той же причине, что и word_hits
        # выше.
        compacts_wide = [e["compact"] for e in recent_content if e.get("compact")]
        if len(compacts_wide) >= 3:
            candidate_compact = compacts_wide[-1]
            char_hits = sum(
                1
                for c in compacts_wide[:-1]
                if self._mod_char_containment(c, candidate_compact) >= 0.7
            )
            if char_hits >= 2:
                score += 3
                reasons.append("char_obfuscation_flood")
            elif char_hits == 1:
                weak_repeat_hint = True

        if weak_repeat_hint:
            # Рейдер, который почти на каждом сообщении меняет "тему"
            # (разные оскорбления/лозунги) и лишь изредка повторяет
            # одну и ту же 2-3 раза, не успевает набрать полноценные
            # near_duplicate_flood/char_obfuscation_flood (нужно >=2
            # прошлых похожих) на КАЖДОМ сообщении — но единичное
            # совпадение по любой из двух метрик (словесной ИЛИ
            # посимвольной) всё равно не случайно на фоне остального
            # разнородного флуда. Небольшой, но реальный вклад в
            # score — как раз то, чего этому сигналу не хватало на
            # реальном инциденте, где "полных" повторов было мало, а
            # тем не менее нужный итоговый вывод был очевиден человеку
            # с первого взгляда на весь диалог целиком.
            score += 1
            reasons.append("repeated_phrase_hint")

        lower = str(text or "").casefold()
        url_count = len(re.findall(r"https?://|www\.", lower))
        if url_count >= 3 or (url_count and len(recent_15) >= 6):
            score += 2
            reasons.append("link_flood")

        if len(text) >= 4000:
            score += 2
            reasons.append("oversized")
        elif len(text) >= 2000:
            score += 1
            reasons.append("long_message")

        # "Массовые короткие сообщения вместо одного длинного" (п.13
        # ТЗ) — отдельный, более чувствительный порог, чем burst_*:
        # 4 коротких сообщения за 15с ещё не добивают до burst_15s(8),
        # но уже характерны для флуда короткими репликами.
        if len(recent_15) >= 4:
            short_count = sum(
                1 for e in recent_15 if len(str(e.get("text") or "")) < 20
            )
            if short_count >= 4:
                score += 1
                reasons.append("short_message_flood")

        # Сильный сигнал автоматизации, но сам по себе НЕ достаточен.
        identity = f"{nick} {occupant_jid or ''}"
        if self.MOD_BOT_HINT_RE.search(identity):
            score += 2
            reasons.append("bot_identity_hint")

        # Чем меньше средний интервал, тем сильнее автоматизация.
        if len(recent_15) >= 6:
            span = max(0.01, recent_15[-1]["t"] - recent_15[0]["t"])
            avg_interval = span / max(1, len(recent_15) - 1)
            if avg_interval < 0.55:
                score += 2
                reasons.append("machine_pacing")
            elif avg_interval < 1.0:
                score += 1
                reasons.append("fast_pacing")

            # Эвазия "разбавить среднее одной длинной паузой": медиана
            # интервалов всё ещё очень маленькая, даже если среднее
            # формально не попало в machine_pacing/fast_pacing выше.
            # Рейдер, который чередует короткие и длинные паузы именно
            # чтобы обойти порог по среднему, эту медиану не обманет.
            deltas = sorted(
                recent_15[i]["t"] - recent_15[i - 1]["t"]
                for i in range(1, len(recent_15))
            )
            median_interval = deltas[len(deltas) // 2]
            if median_interval < 0.5 and avg_interval >= 1.0:
                score += 2
                reasons.append("pacing_evasion")

        # Доля потока комнаты, которую занимает этот автор (п.3 ТЗ).
        # Считаем только когда в комнате вообще есть заметный трафик —
        # иначе один активный человек в тихой комнате всегда "100%
        # потока", что ничего не говорит о рейде.
        if room_recent_count and room_recent_count >= 8:
            share = len(recent_15) / room_recent_count
            if share >= 0.6:
                score += 2
                reasons.append("room_share_dominant")
            elif share >= 0.4:
                score += 1
                reasons.append("room_share_high")

        return score, reasons

    def _mod_ban_eligible(self, reasons, events):
        """
        Доп. структурный гейт ПОВЕРХ score >= MODERATION_SCORE_BAN.

        Короткий, но очень интенсивный всплеск от живого человека
        (несколько сообщений подряд) может сам по себе легко дать
        score >= MODERATION_SCORE_BAN — но НЕ должен в одиночку
        приводить к необратимому бану. Нужно либо устойчивое число
        сообщений в окне (реальный длящийся поток, а не всплеск),
        либо явный bot-identity-hint в нике вместе с несколькими
        независимыми поведенческими сигналами. Это прямое продолжение
        главного требования ТЗ — "не начинает банить обычных
        пользователей" — перенесённое из исходной _mod_bot_confidence.
        """
        if len(events) >= MODERATION_BAN_SUSTAINED_EVENTS:
            return True

        if "bot_identity_hint" in reasons:
            n = sum(1 for r in reasons if r in self.MOD_BOT_SIGNAL_REASONS)
            if n >= MODERATION_BAN_IDENTITY_SIGNALS:
                return True

        return False

    def _mod_level(self, score, reasons, events, llm_bot_confirmed):
        """
        Единая точка принятия решения об уровне реакции — заменяет
        старые параллельные human_aggressive/bot_aggressive ветки.
        Score монотонно определяет уровень; единственное исключение —
        подтверждение LLM (llm_bot_confirmed), которое форсирует "ban"
        напрямую, даже если числовые эвристики всю дорогу молчали
        (см. докстроку _mod_llm_check про намеренно медленных ботов,
        которых burst/duplicate-окна физически не могут увидеть).
        """
        if llm_bot_confirmed:
            return "ban"

        if score < MODERATION_SCORE_WATCH:
            return "none"
        if score < MODERATION_SCORE_RESTRICT:
            return "watch"
        if score < MODERATION_SCORE_DELETE:
            return "restrict"

        if score >= MODERATION_SCORE_BAN and self._mod_ban_eligible(
            reasons, events
        ):
            return "ban"

        return "delete"

    async def _mod_tenure(self, key, sender, occupant_jid, room_jid):
        """
        Стаж участника в этой комнате по chat_archive (п.9-11 ТЗ):
        сколько раз он уже писал здесь и когда появился впервые.
        Позволяет отличать давнего участника, у которого случился
        один интенсивный всплеск, от ника, который появился только
        что и сразу флудит — и снижать/повышать score соответственно
        (см. интеграцию в moderate_incoming_muc).

        Кэшируется на весь процесс на (room, ключ): стаж только
        растёт, гонять DB-запрос на каждое сообщение бессмысленно —
        тот же принцип, что и у _mod_llm_check/mod_llm_state.
        Отсутствие self.db, выключенный CHAT_ARCHIVE или любая ошибка
        БД трактуются как "стаж неизвестен" (None) — вызывающий код
        обязан в этом случае просто не менять score, а не наказывать
        за недоступность архива.
        """
        if not hasattr(self, "db"):
            return None

        cached = self.mod_tenure_cache.get(key)
        if cached is not None:
            return cached

        try:
            stats = await self.db.get_room_sender_stats(
                room_jid, sender, occupant_jid
            )
        except Exception:
            logging.debug(
                "[МОДЕРАЦИЯ] Не удалось получить стаж по архиву для %s",
                sender,
                exc_info=True,
            )
            return None

        if stats is None:
            return None

        self.mod_tenure_cache[key] = stats
        return stats

    def _mod_llm_context(self, events):
        """
        Последние MODERATION_LLM_CONTEXT_MESSAGES сообщений этого
        ника + интервалы между ними (в секундах) — ровно то, что
        уходит в classify_possible_bot(). Берём хвост events (они
        уже отсортированы по времени по построению — см. append в
        moderate_incoming_muc).
        """
        tail = list(events)[-MODERATION_LLM_CONTEXT_MESSAGES:]
        messages = [e["text"] for e in tail]

        intervals = [
            tail[i]["t"] - tail[i - 1]["t"]
            for i in range(1, len(tail))
        ]

        return messages, intervals

    async def _mod_llm_check(self, key, sender, events, room_jid, score):
        """
        Решает, нужен ли сейчас LLM-запрос, и если да — делает его и
        кэширует вердикт в self.mod_llm_state.

        Два НЕЗАВИСИМЫХ условия для вызова ("eager"):
          1) score попал в серую зону
             (MODERATION_LLM_GREYZONE_MIN <= score < MODERATION_SCORE_DELETE)
             — ровно то, о чём просит ТЗ: LLM как арбитр, когда
             эвристики сами не уверены. score ниже зоны — эвристики
             и так говорят "всё в порядке"; score на уровне DELETE и
             выше — решение об удалении уже очевидно без LLM.
          2) ключ находится в "чувствительном окне" после RESTRICT-
             уровня (mod_restricted_until) — участник уже один раз
             показался подозрительным, поэтому следующая проверка
             не ждёт обычного каданса.
        Без этих условий — старый кадансный fallback
        (EVERY_MESSAGES/COOLDOWN): ловит НАМЕРЕННО медленного бота
        (паузы ~15-20с), который burst/duplicate-окна физически не
        могут затолкать в серую зону по score — см. докстроку класса
        и smoke-тест "LLM ловит медленного бота".

        Каданс считается через self.mod_llm_pending[key] — счётчик
        "сообщений с последней проверки", инкрементируемый на каждый
        вызов НЕЗАВИСИМО от текущей длины events. Раньше (и в
        исходном коде тоже) каданс считался как
        `len(events) - messages_at_check_на_момент_прошлой_проверки` —
        это ломается, как только между сообщениями раздора случается
        пауза длиннее ретеншена events (MODERATION_CONTENT_WINDOW_SECONDS/
        MODERATION_WINDOW_SECONDS): events обнуляется, а
        messages_at_check остаётся прежним, счётчик уходит в минус и
        каданс больше НИКОГДА не наступает — LLM-арбитр перестаёт
        вызываться навсегда после первой же такой паузы. Именно это
        произошло в проде: рейдер держал паузы ~10-30с (иногда больше
        3 минут), первая проверка на 8-м сообщении случилась, а
        дальше — тишина, хотя флуд продолжался ещё 15+ сообщений.

        Возвращает {"bot":.., "confidence":.., "reason":..} только
        когда проверка РЕАЛЬНО была выполнена в этом вызове — не
        путать с "бота нет": для промежуточных сообщений между
        проверками, а также при недоступности/ошибке LLM, возвращает
        None.
        """
        if not MODERATION_LLM_ENABLED:
            self._mod_notice_inactive(room_jid, "llm_disabled")
            return None

        if len(events) < MODERATION_LLM_MIN_MESSAGES:
            return None

        now = time.monotonic()
        self.mod_llm_pending[key] += 1

        state = self.mod_llm_state.get(key)
        checked_at = state["checked_at"] if state is not None else None

        grey_zone = MODERATION_LLM_GREYZONE_MIN <= score < MODERATION_SCORE_DELETE
        sensitized = self.mod_restricted_until.get(key, 0.0) > now
        eager = grey_zone or sensitized

        if not eager and self.mod_llm_pending[key] < MODERATION_LLM_EVERY_MESSAGES:
            return None

        if (
            checked_at is not None
            and now - checked_at < MODERATION_LLM_COOLDOWN_SECONDS
        ):
            return None

        messages, intervals = self._mod_llm_context(events)

        verdict = await self.classify_possible_bot(
            sender,
            messages,
            intervals,
        )

        # Кешируем факт проверки даже при verdict=None (ошибка/
        # таймаут LLM) — иначе временная недоступность провайдера
        # превратилась бы в LLM-запрос на КАЖДОЕ следующее сообщение
        # этого ника, что и дорого, и противоречит смыслу кадансации.
        self.mod_llm_state[key] = {"checked_at": now, "verdict": verdict}
        self.mod_llm_pending[key] = 0

        return verdict

    async def _mod_retract(self, room_jid, stanza_id, reason):
        if not stanza_id:
            logging.warning(
                "[МОДЕРАЦИЯ] Нет MUC stanza-id — не могу удалить сообщение"
            )
            return False

        try:
            plugin = self.plugin["xep_0425"]
            if plugin is None:
                logging.error(
                    "[МОДЕРАЦИЯ] XEP-0425 не зарегистрирован"
                )
                return False

            await plugin.moderate(
                JID(room_jid),
                stanza_id,
                reason,
            )
            return True
        except Exception:
            logging.exception(
                "[МОДЕРАЦИЯ] Не удалось удалить stanza-id=%s",
                stanza_id,
            )
            return False

    async def _mod_retract_recent(self, room_jid, jid, nick, reason):
        """
        Удаляет недавние сообщения закреплённого агрессора из
        quote_lookback. Уже не попавшие в lookback сообщения физически
        недоступны через клиент — их можно удалить только через MAM/
        серверную модерацию, если сервер предоставляет такой API.
        """
        target_jid = str(jid or "").casefold()
        target_nick = str(nick or "").casefold()

        candidates = []

        for entry in list(self.quote_lookback):
            sender = str(entry.get("sender") or "")
            sender_jid = str(entry.get("sender_jid") or "")
            if (
                (target_jid and sender_jid.casefold() == target_jid)
                or (
                    not target_jid
                    and sender.casefold() == target_nick
                )
            ):
                candidates.append(entry)

        deleted = 0
        for entry in candidates:
            sid = entry.get("stanza_id")
            if await self._mod_retract(
                room_jid,
                sid,
                reason,
            ):
                deleted += 1

        return deleted

    async def moderate_incoming_muc(self, msg, sender, text, room_jid):
        """
        Возвращает True, если сообщение уже обработано модерацией и
        основной pipeline MUC должен прекратить обработку.

        Уровни реакции по единой шкале score (см. _mod_level):
          none    — ничего не делать;
          watch   — только лог, сообщение не трогаем ("подозрение");
          restrict — удаляем текущее flood-сообщение, один раз просим
                     "быть вменяемым", отмечаем ключ как "под
                     приглядом" на MODERATION_RESTRICT_SECONDS (это
                     ускоряет следующую LLM-проверку, но НЕ приводит
                     к тихому удалению будущих нормальных сообщений —
                     см. _mod_llm_check);
          delete  — удаляем текущее И недавние сообщения этого ключа,
                     но НЕ фиксируем постоянно — при следующем
                     сообщении ключ оценивается заново;
          ban     — как delete, но постоянно фиксируем JID/ник как
                     рейдера — дальше все его сообщения удаляются без
                     повторной оценки (см. блок fixed_bot ниже).

        Кроме числовых эвристик (_mod_score) периодически подключается
        лёгкий LLM-классификатор (_mod_llm_check) — он ловит
        намеренно медленный, семантически узнаваемый спам, который
        не попадает под burst/pacing-окна. LLM только классифицирует;
        решение принимает код через _mod_level/MODERATION_LLM_*
        пороги (см. config.py и llm.py).
        """
        if not MODERATION_ENABLED:
            self._mod_notice_inactive(room_jid, "disabled")
            return False

        if not self._mod_is_privileged(room_jid):
            self._mod_notice_inactive(room_jid, "not_privileged")
            return False

        if str(sender).casefold() == self.nick.casefold():
            return False

        now = time.monotonic()

        # Трафик комнаты в целом (для room_share_* в _mod_score) —
        # считаем ДО фильтрации по fixed_bot, т.к. поток уже
        # закреплённого рейдера тоже часть реальной нагрузки на
        # комнату и не должен занижать знаменатель для остальных.
        room_key = str(room_jid or "").casefold()
        room_events = self.mod_room_events[room_key]
        room_events.append(now)
        while room_events and now - room_events[0] > MODERATION_WINDOW_SECONDS:
            room_events.popleft()

        occupant_jid = self._mod_occupant_jid(room_jid, sender)
        key = self._mod_key(occupant_jid, sender)

        # Вайтлист (!вайтлист, см. commands.py): проверяем ДО
        # fixed_bot, а не после — иначе снять с кого-то уже
        # поставленный автоматический бан через вайтлист было бы
        # невозможно (fixed_bot вернул бы True раньше и до этой
        # проверки очередь бы не дошла).
        canonical = self._mod_canonical_jid(occupant_jid) if occupant_jid else None
        if canonical and canonical in self.mod_whitelist_jids:
            if canonical in self.mod_fixed_jids:
                self.mod_fixed_jids.discard(canonical)
                logging.info(
                    "[МОДЕРАЦИЯ] jid=%s в вайтлисте — снята прошлая "
                    "фиксация бота",
                    canonical,
                )
            return False

        # Уже вычисленный бот: без повторной классификации.
        fixed_bot = (
            (
                occupant_jid
                and self._mod_canonical_jid(occupant_jid)
                in self.mod_fixed_jids
            )
            or (
                not occupant_jid
                and sender.casefold() in self.mod_fixed_nicks
            )
        )
        if fixed_bot:
            sid = self.extract_stanza_id(msg)
            await self._mod_retract(
                room_jid,
                sid,
                "Автоматическая модерация: рейд/бот",
            )
            return True

        events = self.mod_events[key]
        events.append(
            {
                "t": now,
                "norm": self._mod_normalize(text),
                "compact": self._mod_compact(text),
                "stanza_id": self.extract_stanza_id(msg),
                "text": str(text),
                "sender": sender,
                "sender_jid": occupant_jid,
            }
        )

        # Отбрасываем старые события, но оставляем достаточно для
        # удаления последних 60 сообщений через quote_lookback, для
        # LLM-контекста при паузах между сообщениями
        # (MODERATION_LLM_CONTEXT_WINDOW_SECONDS) и для сигналов
        # повтора контента (MODERATION_CONTENT_WINDOW_SECONDS) — иначе
        # события удаляются из mod_events раньше, чем их успевает
        # увидеть более широкое из этих трёх окон.
        retention = max(
            MODERATION_WINDOW_SECONDS,
            MODERATION_LLM_CONTEXT_WINDOW_SECONDS,
            MODERATION_CONTENT_WINDOW_SECONDS,
        )
        while events and now - events[0]["t"] > retention:
            events.popleft()

        room_recent_count = sum(1 for t in room_events if now - t <= 15)

        score, reasons = self._mod_score(
            list(events), text, sender, occupant_jid, room_recent_count
        )

        # ------------------------------------------------
        # АРХИВ: стаж участника в этой комнате (п.9-11 ТЗ). Новый
        # ник без единой строки в chat_archive этой комнаты чуть
        # подозрительнее сам по себе; давний, активно писавший здесь
        # участник — наоборот, получает небольшую скидку, чтобы
        # один интенсивный всплеск не выглядел как рейд.
        # ------------------------------------------------
        tenure = await self._mod_tenure(key, sender, occupant_jid, room_jid)
        if tenure:
            if tenure.get("message_count", 0) == 0:
                score += 1
                reasons.append("no_room_history")
            else:
                first_seen = tenure.get("first_seen")
                if (
                    tenure["message_count"] >= MODERATION_TENURE_TRUSTED_MESSAGES
                    and first_seen
                ):
                    try:
                        first_seen_dt = datetime.fromisoformat(str(first_seen))
                        age_days = (
                            datetime.now() - first_seen_dt
                        ).total_seconds() / 86400.0
                    except ValueError:
                        age_days = 0.0
                    if age_days >= MODERATION_TENURE_TRUSTED_DAYS:
                        score = max(0, score - MODERATION_TENURE_DAMPEN)
                        reasons.append("trusted_history")

        # ------------------------------------------------
        # СЕМАНТИЧЕСКИЙ СЛОЙ: LLM смотрит на последние сообщения
        # этого ника и решает РОВНО ОДНО — bot true/false + confidence
        # (см. _mod_llm_check про условия вызова). Решение, что с
        # этим делать, всё равно принимает код ниже, а не сама LLM.
        # ------------------------------------------------
        llm_bot_confirmed = False
        llm_verdict = await self._mod_llm_check(
            key, sender, events, room_jid, score
        )

        if llm_verdict and llm_verdict["bot"]:
            if llm_verdict["confidence"] >= MODERATION_LLM_BOT_CONFIDENCE:
                llm_bot_confirmed = True
                reasons.append("llm_bot_confirmed")
            elif llm_verdict["confidence"] >= MODERATION_LLM_WATCH_CONFIDENCE:
                score += 2
                reasons.append("llm_bot_suspect")

        level = self._mod_level(score, reasons, events, llm_bot_confirmed)

        # "Три предупреждения" — если ОДИН И ТОТ ЖЕ ключ уже несколько
        # раз за последнее время получал restrict, а раздражитель не
        # прекращается — это само по себе сильная улика, НЕЗАВИСИМАЯ
        # от того, что скажет LLM в КОНКРЕТНО этот раз. Нужна для
        # случая, когда участник умело балансирует прямо на грани:
        # то заходит в watch/restrict, то откатывается, а LLM раз за
        # разом (обоснованно осторожно) не даёт уверенного bot=true
        # ни по одному отдельному сообщению — но САМ ФАКТ, что человек
        # снова и снова возвращается в restrict в течение получаса,
        # эвристики и LLM вместе просто не могут объяснить "случайным
        # совпадением" бесконечно. Считается только для restrict
        # (watch — это ещё "может, ничего", а delete/ban это уже
        # активное действие само по себе).
        if level == "restrict":
            strike = self.mod_restrict_strikes.get(key)
            if (
                strike is None
                or now - strike["last_at"] > MODERATION_RESTRICT_STRIKE_WINDOW_SECONDS
            ):
                strike = {"count": 0, "last_at": now}
            strike["count"] += 1
            strike["last_at"] = now
            self.mod_restrict_strikes[key] = strike

            if strike["count"] >= MODERATION_RESTRICT_STRIKES_TO_ESCALATE:
                level = "ban"
                reasons.append("repeat_offender")

        if level in ("none", "watch"):
            if level == "watch":
                logging.info(
                    "[МОДЕРАЦИЯ] Подозрение nick=%s jid=%s score=%s "
                    "reasons=%s — пока без действий",
                    sender,
                    occupant_jid or "?",
                    score,
                    ",".join(reasons),
                )
            return False

        # restrict и delete — оба ещё "не уверены, что это бот", и в
        # обоих случаях (как и в исходной human-ветке) автору один раз
        # мягко пишем "будь вменяемым" — предупреждение не повторяется
        # чаще раза на ключ. Только ban НЕ предупреждает: к этому
        # моменту решение уже принято окончательно.
        if level in ("restrict", "delete"):
            warn_key = (
                occupant_jid.casefold() if occupant_jid else sender.casefold()
            )
            if warn_key not in self.mod_warned_humans:
                self.mod_warned_humans.add(warn_key)
                self.send_message(
                    mto=room_jid,
                    mbody=(
                        f"{sender}, угомонись немного. "
                        "Пожалуйста, без флуда и рейдерства — будь вменяемым."
                    ),
                    mtype="groupchat",
                )

        if level == "restrict":
            sid = self.extract_stanza_id(msg)
            await self._mod_retract(
                room_jid,
                sid,
                "Временное ограничение: подозрение на флуд/рейд",
            )
            self.mod_restricted_until[key] = now + MODERATION_RESTRICT_SECONDS

            logging.info(
                "[МОДЕРАЦИЯ] Временное ограничение nick=%s jid=%s "
                "score=%s reasons=%s — удалено одно сообщение, "
                "включён ускоренный LLM-контроль на %.0fс",
                sender,
                occupant_jid or "?",
                score,
                ",".join(reasons),
                MODERATION_RESTRICT_SECONDS,
            )
            return True

        # level in ("delete", "ban") — общее действие: удаляем текущее
        # и недавние сообщения этого ключа. Отличие только в том,
        # фиксируем ли мы ключ постоянно.
        if level == "ban":
            if occupant_jid:
                canonical_jid = self._mod_canonical_jid(occupant_jid)
                if canonical_jid:
                    self.mod_fixed_jids.add(canonical_jid)
            else:
                self.mod_fixed_nicks.add(sender.casefold())

        logging.warning(
            "[МОДЕРАЦИЯ] %s nick=%s jid=%s score=%s reasons=%s",
            "БОТ-РЕЙДЕР" if level == "ban" else "Удаление флуда",
            sender,
            occupant_jid or "?",
            score,
            ",".join(reasons),
        )

        deleted = await self._mod_retract_recent(
            room_jid,
            occupant_jid,
            sender,
            "Автоматическая модерация: бот-рейдер"
            if level == "ban"
            else "Автоматическая модерация: флуд",
        )

        # На случай отсутствия текущего сообщения в lookback:
        sid = self.extract_stanza_id(msg)
        if sid:
            await self._mod_retract(
                room_jid,
                sid,
                "Автоматическая модерация: бот-рейдер"
                if level == "ban"
                else "Автоматическая модерация: флуд",
            )

        if level == "ban":
            logging.info(
                "[МОДЕРАЦИЯ] Закреплён бот-рейдер jid=%s nick=%s; удалено=%d",
                occupant_jid or "?",
                sender,
                deleted,
            )
        else:
            logging.info(
                "[МОДЕРАЦИЯ] Удалены недавние сообщения nick=%s jid=%s; "
                "удалено=%d (без постоянной фиксации)",
                sender,
                occupant_jid or "?",
                deleted,
            )

        return True

    # ============================================================
    # !вайтлист — управление вайтлистом модерации по bare JID
    # ============================================================
    #
    # Доступ ТОЛЬКО по подтверждённому bare JID вызывающего
    # (MODERATION_WHITELIST_ADMIN_JIDS, config.py) — не по нику,
    # см. обоснование в config.py рядом с этой константой и в
    # докстроке _mod_occupant_jid выше. Ник можно присвоить себе
    # заново после выхода настоящего владельца; bare JID Пёс видит
    # только через occupant-метаданные MUC, пока сам модератор/
    # админ комнаты — подделать его рядовому участнику нельзя.

    async def handle_moderation_whitelist_command(self, sender, text, room_jid):
        """
        !вайтлист — список текущих записей.
        !вайтлист добавить <ник_или_jid>
        !вайтлист убрать <ник_или_jid>

        <ник_или_jid>: если аргумент содержит "@" — считаем его
        готовым bare JID; иначе ищем ник среди occupant'ов комнаты
        прямо сейчас (тем же _mod_occupant_jid, что и вся
        остальная модерация) — для этого цель должна быть онлайн
        в комнате в момент вызова команды.
        """
        actor_jid = self._mod_occupant_jid(room_jid, sender)
        actor_canonical = (
            self._mod_canonical_jid(actor_jid) if actor_jid else None
        )

        # Ответы !вайтлист всегда идут ЛИЧНО вызвавшему (mtype="chat"),
        # а не в комнату — это управляющая команда, не нужно спамить
        # весь чат подтверждениями. room_jid здесь — голый JID комнаты
        # (нужен именно такой для _mod_occupant_jid выше), поэтому
        # адрес личного ответа собираем сами, а не берём room_jid как
        # есть.
        reply_jid = f"{room_jid}/{sender}"

        if (
            not actor_canonical
            or actor_canonical not in MODERATION_WHITELIST_ADMIN_JIDS
        ):
            self.send_message(
                mto=reply_jid,
                mbody=(
                    "❌ Вайтлистом модерации распоряжаются только "
                    "доверенные по JID — тебя в этом списке нет."
                ),
                mtype="chat",
            )
            return

        parts = text.strip().split(None, 2)
        sub = parts[1].casefold() if len(parts) > 1 else "список"

        if sub in ("список", "list", ""):
            entries = await self.db.moderation_whitelist_list()
            if not entries:
                self.send_message(
                    mto=reply_jid,
                    mbody="Вайтлист модерации пуст.",
                    mtype="chat",
                )
                return
            lines = "\n".join(
                f"• {e['jid']} (добавил: {e['added_by'] or '?'})"
                for e in entries
            )
            self.send_message(
                mto=reply_jid,
                mbody=f"📋 Вайтлист модерации:\n{lines}",
                mtype="chat",
            )
            return

        if sub not in ("добавить", "add", "убрать", "remove", "удалить"):
            self.send_message(
                mto=reply_jid,
                mbody=(
                    "Формат: !вайтлист [добавить|убрать] "
                    "<ник_или_jid> (без аргументов — список)."
                ),
                mtype="chat",
            )
            return

        if len(parts) < 3 or not parts[2].strip():
            self.send_message(
                mto=reply_jid,
                mbody=f"Формат: !вайтлист {parts[1]} <ник_или_jid>",
                mtype="chat",
            )
            return

        target_raw = parts[2].strip()

        if "@" in target_raw:
            target_jid = self._mod_canonical_jid(target_raw)
        else:
            resolved = self._mod_occupant_jid(room_jid, target_raw)
            target_jid = (
                self._mod_canonical_jid(resolved) if resolved else None
            )
            if not target_jid:
                self.send_message(
                    mto=reply_jid,
                    mbody=(
                        f"❌ Не вижу реальный JID «{target_raw}» — "
                        "участника нет в комнате прямо сейчас. "
                        "Можно указать JID напрямую."
                    ),
                    mtype="chat",
                )
                return

        if sub in ("добавить", "add"):
            await self.db.moderation_whitelist_add(
                target_jid, added_by=actor_canonical
            )
            self.mod_whitelist_jids.add(target_jid)
            self.mod_fixed_jids.discard(target_jid)
            self.send_message(
                mto=reply_jid,
                mbody=f"✅ {target_jid} добавлен в вайтлист модерации.",
                mtype="chat",
            )
            return

        removed = await self.db.moderation_whitelist_remove(target_jid)
        self.mod_whitelist_jids.discard(target_jid)
        if removed:
            self.send_message(
                mto=reply_jid,
                mbody=f"✅ {target_jid} убран из вайтлиста модерации.",
                mtype="chat",
            )
        else:
            self.send_message(
                mto=reply_jid,
                mbody=f"«{target_jid}» и так не было в вайтлисте.",
                mtype="chat",
            )
